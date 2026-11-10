"""Manage the RAG Compose project using the selected container CLI."""

import argparse
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from itertools import zip_longest
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener


ROOT = Path(__file__).resolve().parent
PROJECT = "rag-hackathon"
# Groups are started in order and stopped in reverse order.
GROUPS = {
    "infra": ("postgres", "redis", "rabbitmq", "qdrant"),
    "app": ("core", "worker"),
    "monitoring": ("loki", "alloy", "grafana"),
    "load": ("locust",),
}
SERVICES = tuple(service for group in GROUPS.values() for service in group)
SCENARIOS = {"infra": ("infra",), "app": ("infra", "app"), "full": tuple(GROUPS)}
APP = GROUPS["app"]
SCAFFOLD_IMAGES = {
    "core": "localhost/rag-core:scaffold",
    "worker": "localhost/rag-worker:scaffold",
}
REQUIRED_HEALTH = {"postgres", "redis", "rabbitmq", "qdrant", "core"}
# Container TCP ports and optional browser paths.
ENDPOINTS = {
    "postgres": {5432: None},
    "redis": {6379: None},
    "rabbitmq": {5672: None},
    "qdrant": {6333: "/dashboard", 6334: None},
    "core": {8000: "/health/ready"},
    "loki": {3100: "/ready"},
    "alloy": {12345: "/"},
    "grafana": {3000: "/"},
    "locust": {8089: "/"},
}
PASSWORD_KEYS = {
    "postgres": ("POSTGRES_PASSWORD", "POSTGRES_PASSWORD"),
    "rabbitmq": ("RABBITMQ_PASSWORD", "RABBITMQ_DEFAULT_PASS"),
    "grafana": ("GRAFANA_PASSWORD", "GF_SECURITY_ADMIN_PASSWORD"),
}


class ManagerError(Exception):
    pass


class CommandTimeout(ManagerError):
    pass


def run(command, *, capture=False, timeout=None):
    try:
        result = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            capture_output=capture,
            text=capture,
            encoding="utf-8" if capture else None,
            errors="replace" if capture else None,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise CommandTimeout(f"{command[0]} timed out after {timeout}s.") from exc
    except OSError as exc:
        raise ManagerError(f"{command[0]}: {exc}") from exc
    if result.returncode:
        detail = (result.stderr or result.stdout or "").strip()
        raise ManagerError(detail or f"{command[0]} exited with code {result.returncode}.")
    return result.stdout or ""


def engine_candidates(args):
    if args.engine != "podman" and shutil.which("docker"):
        if args.context:
            yield ["docker", "--context", args.context]
        elif os.environ.get("DOCKER_HOST") and not os.environ.get("DOCKER_CONTEXT"):
            yield ["docker"]
        else:
            try:
                contexts = run(
                    ["docker", "context", "ls", "--format", "{{.Name}}"],
                    capture=True, timeout=5,
                ).splitlines()
            except ManagerError:
                contexts = []
            for name in dict.fromkeys(contexts):
                yield ["docker", "--context", name]
            if not contexts:
                yield ["docker"]
    if not args.context and args.engine != "docker" and shutil.which("podman"):
        yield ["podman"]


def available_engines(args, report):
    available = []
    for command in engine_candidates(args):
        label = " ".join(command)
        try:
            run([*command, "info", "--format", "json" if command[0] == "podman" else "{{json .}}"],
                capture=True, timeout=6)
            run([*command, "compose", "version"], capture=True, timeout=12)
        except ManagerError as exc:
            report(f"{label}: {exc}")
        else:
            available.append(command)
    return available


def select_engine(args):
    available = available_engines(args, lambda message: print(message, file=sys.stderr))
    if not available:
        raise ManagerError("No usable engine and Compose provider. Install Docker with Compose or Podman with a Compose provider, then start the engine.")
    if len(available) == 1:
        return available[0]
    if not sys.stdin.isatty() or args.action:
        names = ", ".join(" ".join(command) for command in available)
        raise ManagerError(f"Select an engine with --engine and, for Docker, --context. Available CLI connections: {names}")
    for index, command in enumerate(available, 1):
        print(f"{index}. {' '.join(command)}")
    selected = input("Connection number: ").strip()
    if not selected.isdecimal() or not 1 <= int(selected) <= len(available):
        raise ManagerError("Unknown connection.")
    return available[int(selected) - 1]


@contextmanager
def watch_keys(timeout=3):
    """Read one key at a time on a terminal; wait quietly when redirected."""
    if not sys.stdin.isatty():
        yield lambda: time.sleep(timeout)
    elif os.name == "nt":
        import msvcrt

        def read_key():
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if msvcrt.kbhit():
                    key = msvcrt.getwch()
                    if key in ("\x00", "\xe0"):
                        msvcrt.getwch()
                        return None
                    return key.lower()
                time.sleep(min(timeout, 0.025))
            return None

        yield read_key
    else:
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        previous = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)

            def read_key():
                readable, _, _ = select.select([fd], [], [], timeout)
                return os.read(fd, 1).decode("ascii", errors="ignore").lower() if readable else None

            yield read_key
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, previous)


@dataclass
class Container:
    id: str
    state: str
    health: str
    restarts: object
    started: str
    exit_code: object
    ports: dict = field(default_factory=dict)
    healthcheck: list = field(default_factory=list)

    @property
    def running(self):
        return self.state == "running"

    def ready(self, service):
        if service in REQUIRED_HEALTH:
            return self.running and self.health == "healthy"
        return self.running and self.health in ("healthy", "N/A")

    def endpoints(self, service):
        if not self.running:
            return [("-", "-")]
        rows = []
        for port, path in ENDPOINTS.get(service, {}).items():
            local = []
            for binding in self.ports.get(f"{port}/tcp") or []:
                host = binding.get("HostIp") or "127.0.0.1"
                host_port = binding.get("HostPort")
                if not host_port or str(host_port) == "0":
                    continue
                if host == "0.0.0.0":
                    host = "127.0.0.1"
                if ":" in host:
                    host = "[::1]" if host == "::" else f"[{host}]"
                address = f"{host}:{host_port}"
                local.append(f"http://{address}{path}" if path else address)
            rows.extend(zip_longest([f"{service}:{port}"], dict.fromkeys(local), fillvalue=""))
        return [(inside, host or "-") for inside, host in rows] or [("-", "-")]


class Manager:
    def __init__(self, engine, local, progress=None):
        self.engine = engine
        self.local = local
        self.progress = progress
        self.rss_samples = {}
        self.next_rss_sample = 0
        self.prefix = [*engine, "compose", "-p", PROJECT, "-f", str(ROOT / "compose.yaml")]
        if local:
            self.prefix += ["-f", str(ROOT / "compose.local.yaml")]
        self.scaffold_prefix = [*self.prefix, "-f", str(ROOT / "compose.scaffold.yaml"), "--profile", "*"]
        self.prefix += ["--profile", "*"]

    def compose(self, *args, capture=False, timeout=None):
        return run([*self.prefix, *args], capture=capture or self.progress is not None, timeout=timeout)

    def containers(self):
        ids = run(
            [*self.engine, "ps", "-a", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"],
            capture=True, timeout=20,
        ).splitlines()
        if not ids:
            return {}
        # Podman's remote API can wait on container locks during startup.
        inspect_timeout = 60 if self.engine[0] == "podman" else 20
        raw = run([*self.engine, "inspect", *ids], capture=True, timeout=inspect_timeout)
        try:
            inspected = json.loads(raw)
        except ValueError as exc:
            raise ManagerError("Engine inspect did not return JSON.") from exc
        containers = {}
        for item in inspected:
            labels = item["Config"].get("Labels") or {}
            service = labels.get("com.docker.compose.service") or labels.get("io.podman.compose.service")
            if service not in SERVICES or labels.get("com.docker.compose.oneoff", "").lower() == "true":
                continue
            if service in containers:
                raise ManagerError(f"{service} has multiple containers. This project expects one per service.")
            state = item["State"]
            health = state.get("Health") or state.get("Healthcheck") or {}
            ports = (item.get("NetworkSettings") or {}).get("Ports")
            if state["Status"] != "running" or (ports is None and self.engine[0] == "podman"):
                ports = (item.get("HostConfig") or {}).get("PortBindings")
            containers[service] = Container(
                item["Id"], state["Status"], health.get("Status") or "N/A",
                item.get("RestartCount", "N/A"), state.get("StartedAt", "N/A"),
                state.get("ExitCode", "N/A"), ports or {},
                (item["Config"].get("Healthcheck") or {}).get("Test") or [],
            )
        return containers

    def snapshot(self, resources=False):
        containers = self.containers()
        stats = {}
        ids = [item.id for item in containers.values() if item.running]
        if resources and ids:
            try:
                output = run(
                    [*self.engine, "stats", "--no-stream", "--format",
                     "json" if self.engine[0] == "podman" else "{{json .}}", *ids],
                    capture=True, timeout=8,
                )
                if output.lstrip().startswith("["):
                    rows = json.loads(output)
                else:
                    rows = [json.loads(line) for line in output.splitlines() if line.strip()]
                stats = {str(row.get("ID") or row.get("Id") or row.get("id") or ""): row for row in rows}
            except (ManagerError, ValueError):
                pass
            if self.engine[0] == "podman":
                self.add_rss_estimates(containers, stats)
        return containers, stats

    def add_rss_estimates(self, containers, stats):
        missing = {}
        for item in containers.values():
            if item.running and resource_usage(item, stats)[1] == "N/A":
                key = next((key for key in stats if key and item.id.startswith(key)), item.id)
                missing[item.id] = stats.setdefault(key, {})
        now = time.monotonic()
        if missing and (now >= self.next_rss_sample or missing.keys() - self.rss_samples.keys()):
            with ThreadPoolExecutor(max_workers=3) as executor:
                self.rss_samples = dict(zip(missing, executor.map(self.process_rss, missing)))
            self.next_rss_sample = time.monotonic() + 20
        for container_id, row in missing.items():
            row["rss_estimate"] = self.rss_samples.get(container_id)

    def process_rss(self, container_id):
        try:
            output = run([*self.engine, "top", container_id, "-eo", "pid,rss,args"],
                         capture=True, timeout=4)
        except ManagerError:
            return None
        values = []
        for line in output.splitlines()[1:]:
            fields = line.split(maxsplit=2)
            if len(fields) == 3 and fields[0].isdigit() and fields[1].isdigit():
                if fields[2] != "ps -eo pid,rss,args":
                    values.append(int(fields[1]))
        return f"{sum(values) / 1024:.1f} MiB RSS" if values else None

    def status(self, resources=False):
        if self.progress is not None:
            return
        containers, stats = self.snapshot(resources)
        print(f"{'Service':<10} {'State':<12} {'Health':<11} {'CPU':>8}  {'Memory':<25} {'Restarts':>8} {'Exit':>5}  {'Started at':<35} {'Compose network':<16} Host / browser")
        for service in SERVICES:
            item = containers.get(service)
            if item is None:
                print(f"{service:<10} absent")
                continue
            cpu, ram = resource_usage(item, stats) if resources and item.running else ("-", "-")
            code = item.exit_code if item.state in ("exited", "dead") else "-"
            started = item.started if item.running else "-"
            health = item.health if item.running else "-"
            prefix = f"{service:<10} {item.state:<12} {health:<11} {cpu!s:>8}  {ram!s:<25} {item.restarts!s:>8} {code!s:>5}  {started:<35} "
            for index, (inside, host) in enumerate(item.endpoints(service)):
                print(f"{prefix if index == 0 else ' ' * len(prefix)}{inside:<16} {host}")

    def validate(self):
        if len(SERVICES) != len(set(SERVICES)):
            raise ManagerError("Each Compose service must appear in exactly one group in manager.py.")
        defined = set(self.compose("config", "--services", capture=True, timeout=15).splitlines())
        missing = sorted(set(SERVICES) - defined)
        unassigned = sorted(defined - set(SERVICES))
        if missing or unassigned:
            raise ManagerError(
                "Compose services and GROUPS in manager.py differ. "
                f"Missing from Compose: {', '.join(missing) or '-'}. "
                f"Missing from GROUPS: {', '.join(unassigned) or '-'}.")
    def scaffold_services(self, services):
        app_services = [service for service in services if service in SCAFFOLD_IMAGES]
        if not app_services:
            return []
        config = json.loads(self.compose("config", "--format", "json", capture=True, timeout=15))
        return [service for service in app_services
                if config["services"][service]["image"] == SCAFFOLD_IMAGES[service]]

    def build_scaffold(self, services, *, rebuild=False):
        build = []
        for service in services:
            if not rebuild:
                try:
                    run([*self.engine, "image", "inspect", SCAFFOLD_IMAGES[service]],
                        capture=True, timeout=15)
                    continue
                except CommandTimeout:
                    raise
                except ManagerError:
                    pass
            build.append(service)
        if build:
            if self.progress:
                self.progress(f"Building local scaffold images for {', '.join(build)}")
            run([*self.scaffold_prefix, "build", *build], capture=self.progress is not None)

    def wait_ready(self, services, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            try:
                current = self.containers()
            except CommandTimeout:
                if time.monotonic() >= deadline:
                    raise
                time.sleep(2)
                continue
            pending = [service for service in services if service not in current or not current[service].ready(service)]
            if not pending:
                return
            if time.monotonic() >= deadline or any(
                current[service].state in ("exited", "dead")
                or current[service].health == "unhealthy"
                for service in pending if service in current
            ):
                self.status()
                raise ManagerError(f"Services not ready: {', '.join(pending)}. Run logs SERVICE.")
            time.sleep(2)

    def start(self, services):
        if not services:
            return
        current = self.containers()
        existing_infra = [service for service in services if service in current and service in GROUPS["infra"]]
        recreate = []
        if existing_infra and (self.local or "rabbitmq" in existing_infra):
            config = json.loads(self.compose("config", "--format", "json", capture=True, timeout=15))
            for service in existing_infra:
                settings = config["services"][service]
                if service == "rabbitmq" and current[service].healthcheck != settings["healthcheck"]["test"]:
                    recreate.append(service)
                    continue
                for port in settings.get("ports", []) if self.local else ():
                    target = f'{port["target"]}/{port.get("protocol", "tcp")}'
                    expected = (port.get("host_ip", ""), str(port["published"]))
                    bindings = current[service].ports.get(target) or []
                    if not any((item.get("HostIp"), str(item.get("HostPort"))) == expected for item in bindings):
                        recreate.append(service)
                        break
        if recreate:
            if self.progress:
                self.progress(f"Applying Compose settings to {', '.join(recreate)}")
            self.compose("up", "-d", "--no-deps", "--force-recreate", *recreate)
        missing = [service for service in services if service not in recreate
                   and (service not in current or not current[service].running)]
        if missing:
            self.build_scaffold(self.scaffold_services(missing))
            if self.progress:
                self.progress(f"Starting {', '.join(missing)}")
            try:
                self.compose("up", "-d", "--no-deps", "--no-recreate", *missing)
            except ManagerError as exc:
                self.status()
                raise ManagerError(f"{exc} Failed to start {', '.join(missing)}. Run logs SERVICE.") from exc
        if self.progress:
            self.progress(f"Checking {', '.join(services)}")
        self.wait_ready(services)

    def stop(self, services):
        if not services:
            return
        current = self.containers()
        active = [service for service in services if service in current
                  and current[service].state not in ("exited", "stopped", "created", "dead", "configured")]
        if active:
            if self.progress:
                self.progress(f"Stopping {', '.join(active)}")
            self.compose("stop", *active)

    def scenario(self, mode):
        target = SCENARIOS[mode]
        self.validate()
        for name, services in reversed(GROUPS.items()):
            if name not in target:
                self.stop(tuple(reversed(services)))
        for name, services in GROUPS.items():
            if name in target:
                self.start(services)
        self.status()
        if mode == "full":
            return self.check_monitoring()

    def check_monitoring(self):
        """Report service readiness separately from observed log delivery."""
        if self.progress:
            self.progress("Checking log delivery to Loki")
        containers = self.containers()
        alloy = containers.get("alloy")
        if not alloy or not alloy.running:
            raise ManagerError("Alloy is not running. Use monitor on.")
        grafana = containers.get("grafana")
        if not grafana or not grafana.running:
            raise ManagerError("Grafana is not running. Use monitor on.")
        port = run([*self.engine, "port", alloy.id, "12345/tcp"], capture=True, timeout=15).strip()
        if not re.fullmatch(r"127\.0\.0\.1:\d+", port):
            raise ManagerError("Alloy metrics need a published loopback port on this host.")
        opener = build_opener(ProxyHandler({}))
        deadline = time.monotonic() + 35
        loki_ready = False
        counters = None
        while time.monotonic() < deadline:
            try:
                with opener.open(f"http://{port}/metrics", timeout=3) as response:
                    counters = monitoring_counters(response.read().decode())
            except (OSError, URLError):
                pass
            if not loki_ready:
                try:
                    run([*self.engine, "exec", grafana.id, "wget", "-q", "-O", "/dev/null",
                         "http://loki:3100/ready"], capture=True, timeout=5)
                    loki_ready = True
                except ManagerError:
                    pass
            if loki_ready and counters and counters[0] > 0:
                break
            time.sleep(2)
        if not loki_ready:
            raise ManagerError("Loki readiness check failed from Grafana. Check logs loki.")
        if counters is None:
            raise ManagerError("Could not read Alloy metrics. Check logs alloy and its published port.")
        sent, dropped = counters
        if dropped:
            message = f"Monitoring ready. Logs sent: {sent:g}; dropped: {dropped:g}. See logs alloy."
            color = "yellow"
        elif sent:
            message, color = "Monitoring ready. Log delivery verified.", "green"
        else:
            message, color = "Monitoring ready. No log delivery observed yet.", "yellow"
        if self.progress is None:
            print(message)
        return message, color

    def logs(self, service, follow):
        args = ["logs", "--tail", "100"]
        if follow:
            args.append("--follow")
        return self.compose(*args, service)

    def passwords(self):
        if not (ROOT / ".env").is_file():
            raise ManagerError(".env is missing. Restore the original file to view its passwords.")
        try:
            config = json.loads(self.compose("config", "--format", "json", capture=True, timeout=15))
            return {service: config["services"][service]["environment"][env_key]
                    for service, (_, env_key) in PASSWORD_KEYS.items()}
        except (ManagerError, ValueError, KeyError):
            raise ManagerError("Cannot read Compose credentials. Check the settings in .env.") from None

    def watch(self):
        while True:
            with watch_keys() as read_key:
                while True:
                    if sys.stdout.isatty():
                        print("\033[2J\033[H", end="")
                    self.status(resources=True)
                    print("[R] Refresh  [M] Back  [L] Logs  [S] Stop project  [Q] Quit", flush=True)
                    key = read_key()
                    if key in ("m", "l", "s", "q"):
                        break
            if key in ("m", "q"):
                return key
            if key == "s" and confirm_stop():
                self.execute("stop", None)
            if key == "l":
                try:
                    self.logs(prompt_service(), follow=True)
                except KeyboardInterrupt:
                    print()

    def update(self, source):
        self.validate()
        scaffold = self.scaffold_services(APP)
        registry = [service for service in APP if service not in scaffold]
        if source == "registry" and not registry:
            raise ManagerError("No registry images configured. Use update local to rebuild the scaffold.")
        self.wait_ready(GROUPS["infra"], timeout=0)
        if registry and source in ("auto", "registry"):
            if self.progress:
                self.progress("Downloading application images")
            self.compose("pull", *registry)
        self.build_scaffold(scaffold, rebuild=source in ("auto", "local"))
        self.compose("up", "-d", "--no-deps", "--pull", "never", *APP)
        self.wait_ready(APP)
        self.status()

    def execute(self, action, value):
        if action == "up":
            return self.scenario(value)
        elif action == "stop":
            for services in reversed(GROUPS.values()):
                self.stop(tuple(reversed(services)))
            self.status()
        elif action == "status":
            self.status(resources=True)
        elif action == "watch":
            return self.watch()
        elif action == "restart":
            if value not in self.containers():
                raise ManagerError(f"{value} has no container. Start its scenario first.")
            self.compose("restart", value)
            self.wait_ready((value,))
            self.status()
        elif action in ("monitor", "load"):
            group = GROUPS["monitoring" if action == "monitor" else "load"]
            if value == "off":
                self.stop(tuple(reversed(group)))
                self.status()
            else:
                if value == "on":
                    self.validate()
                    if action == "load":
                        self.wait_ready(("core",), timeout=0)
                    self.start(group)
                    self.status()
                if action == "monitor":
                    return self.check_monitoring()
        elif action == "update":
            self.update(value)


MENU_COMMANDS = (
    ("Infrastructure only", "up", "infra"), ("Application", "up", "app"),
    ("Full diagnostics", "up", "full"), ("Enable monitoring", "monitor", "on"),
    ("Disable monitoring", "monitor", "off"), ("Start Locust", "load", "on"),
    ("Stop Locust", "load", "off"), ("Refresh table", "watch", None),
    ("Service logs", "logs", None), ("Restart service", "restart", None),
    ("Update app images", "update", "auto"), ("Stop project", "stop", None),
    ("Show passwords", "passwords", "show"),
)


def resource_usage(item, stats):
    row = next((row for cid, row in stats.items() if cid and item.id.startswith(cid)), {})
    cpu = row.get("CPUPerc") or row.get("cpu_percent") or row.get("CPU") or "N/A"
    memory = row.get("MemUsage") or row.get("mem_usage") or "N/A"
    if isinstance(memory, str) and memory.partition("/")[0].strip().replace(" ", "") == "0B":
        memory = "N/A"
    if memory == "N/A":
        memory = row.get("rss_estimate") or "N/A"
    return cpu, memory


def monitoring_counters(metrics):
    totals = {"loki_write_sent_entries_total": 0.0, "loki_write_dropped_entries_total": 0.0}
    for line in metrics.splitlines():
        sample = re.match(r"^(loki_write_(?:sent|dropped)_entries_total)(?:\{.*\})?\s+(\S+)", line)
        if sample:
            name, value = sample.groups()
            totals[name] += float(value)
    return totals["loki_write_sent_entries_total"], totals["loki_write_dropped_entries_total"]


def menu(manager):
    commands = MENU_COMMANDS
    while True:
        print()
        for index, (label, _, _) in enumerate(commands, 1):
            print(f"{index:2}. {label}")
        choice = input("Select (0 to exit): ").strip()
        if choice == "0":
            return
        if not choice.isdecimal() or not 1 <= int(choice) <= len(commands):
            print("Unknown option.")
            continue
        _, action, value = commands[int(choice) - 1]
        try:
            if action in ("logs", "restart"):
                value = prompt_service()
            if action == "stop" and not confirm_stop():
                continue
            if action == "passwords":
                show_passwords(manager)
            elif action == "logs":
                manager.logs(value, follow=True)
            elif action == "watch":
                manager.status(resources=True)
            elif manager.execute(action, value) == "q":
                return
        except KeyboardInterrupt:
            print()
        except ManagerError as exc:
            print(exc, file=sys.stderr)


def prompt_service():
    value = input(f"Service ({', '.join(SERVICES)}): ").strip()
    if value not in SERVICES:
        raise ManagerError("Unknown service.")
    return value


def confirm_stop():
    return input(f"Stop all containers of {PROJECT}? [y/N] ").strip().lower() == "y"


def show_passwords(manager):
    if not sys.stdout.isatty():
        raise ManagerError("Show passwords requires an interactive terminal.")
    print("\n".join(password_lines(manager.passwords())))


def password_lines(passwords):
    return [f"{PASSWORD_KEYS[service][0]}={password!r}" for service, password in passwords.items()]


def initialize_env(engine, announce=True):
    path = ROOT / ".env"
    if path.exists():
        return
    volumes = run(
        [*engine, "volume", "ls", "-q", "--filter", f"label=com.docker.compose.project={PROJECT}"],
        capture=True, timeout=20,
    )
    if volumes.strip():
        raise ManagerError("Project volumes already exist. Restore the original .env before starting.")
    settings = [f"{key}={secrets.token_urlsafe(32)}" for key, _ in PASSWORD_KEYS.values()]
    settings.extend(("CORE_IMAGE=", "WORKER_IMAGE="))
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    except OSError as exc:
        raise ManagerError(f"Could not create {path}: {exc}") from exc
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write("# Generated on first run. Keep this file private.\n")
        file.write("\n".join(settings) + "\n")
    if announce:
        print(f"Created {path} with passwords for PostgreSQL, RabbitMQ and Grafana.")


def arguments():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("docker", "podman"))
    parser.add_argument("--context", help="Docker context for this invocation")
    ports = parser.add_mutually_exclusive_group()
    ports.add_argument("--local", dest="local", action="store_true", help="publish dependency ports on loopback")
    ports.add_argument("--no-local", dest="local", action="store_false", help="use the base Compose configuration")
    parser.set_defaults(local=None)
    actions = parser.add_subparsers(dest="action")
    for action, choices in (
        ("up", SCENARIOS), ("monitor", ("on", "off", "check")), ("load", ("on", "off")),
        ("restart", SERVICES), ("update", ("registry", "local")),
    ):
        actions.add_parser(action).add_argument("value", choices=choices)
    for action in ("stop", "status", "watch"):
        actions.add_parser(action)
    logs = actions.add_parser("logs")
    logs.add_argument("service", choices=SERVICES)
    logs.add_argument("-f", "--follow", action="store_true")
    passwords = actions.add_parser("passwords")
    passwords.add_argument("value", choices=("show",))
    args = parser.parse_args()
    if args.local is None:
        args.local = args.action is None
    if args.context and args.engine == "podman":
        parser.error("--context is for Docker; select Podman connections through its CLI.")
    return args


def main():
    args = arguments()
    try:
        if args.action is None and sys.stdin.isatty() and sys.stdout.isatty():
            from dashboard import interactive

            if interactive(args, sys.modules[__name__]):
                return 0
        engine = select_engine(args)
        print(f"{PROJECT} via {' '.join(engine)}")
        if args.action in (None, "up"):
            initialize_env(engine)
        manager = Manager(engine, args.local)
        if args.action is None:
            manager.validate()
            menu(manager)
        elif args.action == "logs":
            manager.logs(args.service, args.follow)
        elif args.action == "passwords":
            show_passwords(manager)
        else:
            manager.execute(args.action, getattr(args, "value", None))
    except (KeyboardInterrupt, EOFError):
        return 130
    except ManagerError as exc:
        print(exc, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
