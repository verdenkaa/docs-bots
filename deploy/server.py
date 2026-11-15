"""Supervise independent HTTP/background processes without changing business contracts."""

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading
import time
from urllib.request import ProxyHandler, build_opener

from rag._configuration import environment_values


def _status_path(role):
    return Path("/tmp/rag-" + role + "-status.json")


def _pid_alive(pid):
    if os.name == "nt" or type(pid) is not int or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        proc = Path("/proc") / str(pid) / "stat"
        return not proc.exists() or proc.read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, ValueError, IndexError):
        return False


def _role_ready(path):
    try:
        return _pid_alive(json.loads(path.read_text(encoding="utf-8"))["pid"])
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _http_ready(role):
    path = "http://127.0.0.1:8000/health/ready" if role == "core" else "http://127.0.0.1:8001/openapi.json"
    try:
        with build_opener(ProxyHandler({})).open(path, timeout=3) as response:
            return response.status == 200
    except OSError:
        return False


def healthy(role):
    try:
        status = json.loads(_status_path(role).read_text(encoding="utf-8"))
        return (status["ready"] is True and 0 <= time.time() - status["updated"] < 15
                and _pid_alive(status["supervisor"])
                and all(_pid_alive(pid) for pid in status["children"])
                and all(_role_ready(Path(path)) for path in status["signals"])
                and _http_ready(role))
    except (OSError, ValueError, KeyError, TypeError):
        return False


def commands(role, values):
    if role not in ("core", "worker"):
        raise ValueError("Unknown deployment role")
    app = "rag.rag_api.application:app" if role == "core" else "rag.ingestion.application:app"
    count = values.get("API_WORKERS", "2") if role == "core" else values.get("INGESTION_HTTP_WORKERS", "1")
    port = "8000" if role == "core" else "8001"
    # The worker heartbeat is independent of long HTTP requests; request budgets
    # remain enforced by the owner modules and the ingestion process supervisor.
    http = [sys.executable, "-m", "gunicorn", app, "--worker-class", "uvicorn_worker.UvicornWorker",
            "--bind", "0.0.0.0:" + port, "--workers", count, "--timeout", "120",
            "--graceful-timeout", "40", "--error-logfile", "-", "--forwarded-allow-ips", "*"]
    if role == "core":
        return {"http": http, **{name: [sys.executable, "-m", "rag.runtime", "serve", name]
                                 for name in ("relay", "recovery", "questions", "results", "maintenance")}}
    return {"http": http, "ingestion": [sys.executable, "-m", "rag.runtime", "worker"]}


def run(role):
    if sys.platform != "linux":
        raise RuntimeError("Server roles require a Linux container")
    values = environment_values()
    stop = threading.Event()
    for signum in (signal.SIGTERM, signal.SIGINT):
        signal.signal(signum, lambda *_: stop.set())
    processes = {}
    signals = []
    status_path = _status_path(role)
    status_path.unlink(missing_ok=True)
    started = time.monotonic()
    was_ready = False
    startup_timeout = float(values.get("RAG_STARTUP_TIMEOUT_S", "600"))
    exit_code = 0
    try:
        for name, command in commands(role, values).items():
            environment = dict(os.environ)
            if name != "http":
                marker = Path("/tmp/rag-" + role + "-" + name + "-ready.json")
                marker.unlink(missing_ok=True)
                signals.append(marker)
                environment["RAG_PROCESS_READY_FILE"] = str(marker)
            processes[name] = subprocess.Popen(command, env=environment)
        print("Started supervised role: " + role, flush=True)
        while not stop.is_set():
            exited = [name for name, process in processes.items() if process.poll() is not None]
            if exited:
                print("Required processes exited: " + ", ".join(exited), file=sys.stderr, flush=True)
                exit_code = 1
                break
            ready = all(_role_ready(path) for path in signals) and _http_ready(role)
            payload = {"ready": ready, "updated": time.time(), "supervisor": os.getpid(),
                       "children": [process.pid for process in processes.values()],
                       "signals": [str(path) for path in signals]}
            temporary = status_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(temporary, status_path)
            if not was_ready and not ready and time.monotonic() - started > startup_timeout:
                print("Role did not become ready within its startup budget: " + role, file=sys.stderr, flush=True)
                exit_code = 1
                break
            # After readiness, temporary capacity/DB failures are reflected in health;
            # they must not kill in-flight work. Process death remains fail-fast.
            if ready:
                was_ready = True
            stop.wait(2)
    finally:
        status_path.unlink(missing_ok=True)
        for process in processes.values():
            if process.poll() is None:
                process.terminate()
        deadline = time.monotonic() + 60
        for process in processes.values():
            try:
                process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        for marker in signals:
            marker.unlink(missing_ok=True)
    return exit_code
