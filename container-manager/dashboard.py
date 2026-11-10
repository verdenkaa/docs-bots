"""Interactive terminal dashboard for the Compose manager."""

import os
import re
import shutil
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from queue import Empty, SimpleQueue
from textwrap import wrap


ESCAPE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
RESET = "\033[0m"
COLORS = {"green": "32", "yellow": "33", "red": "31", "blue": "36", "dim": "2"}


def color(text, name):
    return f"\033[{COLORS[name]}m{text}{RESET}"


def clean(text):
    return "".join(char for char in ESCAPE.sub("", str(text)) if char.isprintable())


@contextmanager
def screen():
    restore = None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetStdHandle.restype = wintypes.HANDLE
        kernel.GetConsoleMode.argtypes = (wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD))
        kernel.SetConsoleMode.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        handle = kernel.GetStdHandle(-11)
        mode = wintypes.DWORD()
        if not kernel.GetConsoleMode(handle, ctypes.byref(mode)) or not kernel.SetConsoleMode(handle, mode.value | 4):
            yield False
            return
        restore = lambda: kernel.SetConsoleMode(handle, mode.value)
    elif os.environ.get("TERM") == "dumb":
        yield False
        return
    try:
        print("\033[?1049h\033[2J\033[?25l", end="", flush=True)
        yield True
    finally:
        print(RESET + "\033[?25h\033[?1049l", end="", flush=True)
        if restore:
            restore()


class Dashboard:
    def __init__(self, args, backend, executor):
        self.args = args
        self.backend = backend
        self.executor = executor
        self.messages = SimpleQueue()
        self.manager = None
        self.engines = []
        self.containers, self.stats = {}, {}
        self.job = self.refresh = None
        self.next_refresh = 0
        self.notice = ("", "dim")
        self.error = ""
        self.phase = ""
        self.prompt = None
        self.entry = ""
        self.view = None
        self.log_lines = []
        self.password_lines = []
        self.scroll = 0
        self.last_frame = None
        self.launch("detect", "Looking for Docker or Podman", self.detect)

    def detect(self):
        diagnostics = []
        engines = self.backend.available_engines(self.args, diagnostics.append)
        if not engines:
            raise self.backend.ManagerError(
                "Start Docker or Podman with a Compose provider.\n" + "\n".join(diagnostics)
            )
        return engines

    def prepare(self, engine):
        self.messages.put("Preparing project configuration")
        self.backend.initialize_env(engine, announce=False)
        manager = self.backend.Manager(engine, self.args.local, progress=self.messages.put)
        manager.validate()
        return manager

    def launch(self, kind, phase, action, *args):
        self.phase = phase
        self.job = (kind, self.executor.submit(action, *args))

    def tick(self):
        while True:
            try:
                self.phase = self.messages.get_nowait()
            except Empty:
                break
        if self.job and self.job[1].done():
            kind, result = self.job
            self.job = None
            try:
                value = result.result()
            except self.backend.ManagerError as exc:
                self.error = str(exc)
                self.notice = ("Operation failed. Press D for details.", "red")
            else:
                if kind == "detect":
                    self.engines = value
                    if len(self.engines) == 1:
                        self.launch("prepare", "Preparing project", self.prepare, self.engines[0])
                elif kind == "prepare":
                    self.manager = value
                    self.notice = ("Ready", "green")
                elif kind == "passwords":
                    self.password_lines = self.backend.password_lines(value)
                    self.view, self.scroll = "passwords", 0
                else:
                    self.notice = value or ("Done", "green")
            self.next_refresh = 0
        if self.refresh and self.refresh[1].done():
            view, result = self.refresh
            self.refresh = None
            try:
                value = result.result()
            except self.backend.ManagerError as exc:
                if not self.error:
                    self.error = str(exc)
                    self.notice = ("Refresh failed. Press D for details.", "red")
            else:
                if view is None:
                    self.containers, self.stats = value
                elif self.view == view:
                    self.log_lines = value.splitlines()
            self.next_refresh = time.monotonic() + 3
        if self.manager and not self.job and not self.refresh and time.monotonic() >= self.next_refresh:
            if self.view is None:
                self.refresh = (None, self.executor.submit(self.manager.snapshot, True))
            elif self.view not in ("details", "passwords"):
                self.refresh = (self.view, self.executor.submit(self.manager.logs, self.view, False))

    def table(self, width):
        endpoints = {service: item.endpoints(service) for service, item in self.containers.items()}
        host_width = max([len("Host / browser")] + [
            len(host) for pairs in endpoints.values() for _, host in pairs
        ])
        service_width = max(len("Service"), *(len(name) for name in self.backend.SERVICES))
        inside_width = max([len("In Compose")] + [
            len(inside) for pairs in endpoints.values() for inside, _ in pairs
        ])
        endpoint_width = inside_width + 2
        header = f"{'Service':<{service_width}} {'State':<10} {'Health':<9}"
        rich = width >= len(header) + 31 + endpoint_width + host_width
        if rich:
            header += f" {'CPU':>7}  {'Memory':<16} {'Exit':>4}"
        prefix_width = len(header)
        stacked = width < prefix_width + endpoint_width + len("Host / browser")
        if not stacked:
            header += f" {'In Compose':<{inside_width}} Host / browser"
        rows = [color(header[:width], "blue"), color("-" * min(width, len(header)), "dim")]
        for service in self.backend.SERVICES:
            item = self.containers.get(service)
            state = item.state if item else "absent"
            health = item.health if item and item.running else "-"
            level = "dim"
            failed_exit = item and state == "exited" and str(item.exit_code) not in ("0", "143")
            if health == "unhealthy" or state == "dead" or failed_exit:
                level = "red"
            elif health == "starting" or state in ("restarting", "paused"):
                level = "yellow"
            elif state == "running":
                level = "green"
            row = f"{service:<{service_width}} {state:<10} {health:<9}"
            if rich:
                cpu, ram = self.backend.resource_usage(item, self.stats) if item and item.running else ("-", "-")
                code = item.exit_code if item and state in ("exited", "dead") else "-"
                row += f" {clean(cpu):>7.7}  {clean(ram):<16.16} {str(code):>4}"
            pairs = endpoints.get(service, [("-", "-")])
            if stacked:
                rows.append(color(row[:width], level))
                for inside, host in pairs:
                    if inside != "-" or host != "-":
                        for address in (f"Compose: {inside}" if inside else "", f"Host: {host}" if host != "-" else ""):
                            rows.extend(color(part, level) for part in wrap(address, width))
                continue
            for inside, host in pairs:
                prefix = f"{row} {inside:<{inside_width}} "
                parts = wrap(host, width - prefix_width - endpoint_width, break_on_hyphens=False)
                rows.append(color(prefix + parts[0], level))
                rows.extend(color(" " * len(prefix) + part, level) for part in parts[1:])
                row = " " * prefix_width
        return rows

    def draw(self):
        cols, height = shutil.get_terminal_size((80, 26))
        width = max(1, cols - 1)

        def line(text, level=None):
            text = clean(text)[:width]
            return color(text, level) if level else text

        engine = " ".join(self.manager.engine) if self.manager else "setup"
        rows = [line(f"RAG / CONTAINER MANAGER   {self.backend.PROJECT}   {engine}", "blue")]
        if self.job:
            spinner = "|/-\\"[int(time.monotonic() * 8) % 4]
            rows.append(line(f"{spinner} {self.phase}", "yellow"))
        else:
            rows.append(line(*self.notice))
        if self.view:
            heading = {"details": "Error details", "passwords": "Configured passwords"}.get(self.view, f"Logs / {self.view}")
            hints = "M Hide   J/K Scroll   0 Exit" if self.view == "passwords" else "M Back   J/K Scroll   R Refresh   0 Exit"
            rows.extend([line(heading, "blue"), line(hints, "dim")])
            content = {"details": self.error.splitlines(), "passwords": self.password_lines}.get(self.view, self.log_lines)
            available = max(1, height - len(rows) - 1)
            wrapped = [text[index:index + width] for raw in content
                       for text in (clean(raw) or " ",) for index in range(0, len(text), width)]
            self.scroll = min(self.scroll, max(0, len(wrapped) - available))
            end = len(wrapped) - self.scroll
            rows.extend(wrapped[max(0, end - available):end] or ["Waiting for output..."])
        elif self.manager:
            commands = self.backend.MENU_COMMANDS
            choices = [f"{i + 1:2} {label}" for i, (label, _, _) in enumerate(commands)]
            rows.extend(self.columns(choices, width, line))
            rows.append(line("R Refresh   D Error details   0 Exit", "dim"))
            if self.prompt in ("logs", "restart"):
                choices = [f"{i + 1} {name}" for i, name in enumerate(self.backend.SERVICES)]
                rows.extend(self.columns(choices, width, line))
            label = {"logs": "Service number", "restart": "Service number",
                     "stop": "Stop project? [y/N]"}.get(self.prompt, "Choice")
            rows.append(line(f"{label} > {self.entry}_", "blue"))
            rows.extend(self.table(width)[:max(0, height - len(rows) - 1)])
        else:
            for index, command in enumerate(self.engines, 1):
                rows.append(line(f"{index}  {' '.join(command)}"))
            rows.extend([line("R Retry   D Error details   0 Exit", "dim"),
                         line(f"Connection > {self.entry}_", "blue")])
        frame = tuple(rows[:max(1, height - 1)])
        if frame != self.last_frame:
            print("\033[H" + "\033[K\n".join(frame) + "\033[K\033[J", end="", flush=True)
            self.last_frame = frame

    @staticmethod
    def columns(items, width, line):
        count = 3 if width >= 66 else 2 if width >= 40 else 1
        cell = width // count
        return [line("".join(f"{text[:cell - 1]:<{cell}}" for text in items[index:index + count]))
                for index in range(0, len(items), count)]

    def enter(self, value):
        if self.prompt == "stop":
            self.prompt = None
            if value == "y":
                self.launch("action", "Stopping project", self.manager.execute, "stop", None)
            return
        if not value.isdecimal() or len(value) > 3:
            self.notice = ("Choose a number from the menu.", "yellow")
            return
        number = int(value)
        if not self.manager:
            if 1 <= number <= len(self.engines):
                self.launch("prepare", "Preparing project", self.prepare, self.engines[number - 1])
            return
        if self.prompt:
            if 1 <= number <= len(self.backend.SERVICES):
                service = self.backend.SERVICES[number - 1]
                if self.prompt == "logs":
                    self.view, self.scroll, self.log_lines = service, 0, []
                    self.next_refresh = 0
                else:
                    self.launch("action", f"Restarting {service}", self.manager.execute, "restart", service)
                self.prompt = None
            return
        if not 1 <= number <= len(self.backend.MENU_COMMANDS):
            return
        label, action, value = self.backend.MENU_COMMANDS[number - 1]
        if action in ("logs", "restart", "stop"):
            self.prompt = action
        elif action == "passwords":
            self.error = ""
            self.launch("passwords", "Reading configured passwords", self.manager.passwords)
        elif action == "watch":
            self.next_refresh = 0
        else:
            self.error = ""
            self.launch("action", label, self.manager.execute, action, value)

    def key(self, key):
        if not key:
            return True
        if key == "q" and not self.job:
            return False
        if key == "d" and self.error and not self.prompt:
            self.view, self.scroll = "details", 0
        elif self.view:
            if key in ("m", "\x1b"):
                self.view = None
                self.password_lines = []
                self.next_refresh = 0
            elif key == "r":
                self.next_refresh = 0
            elif key == "j":
                self.scroll = max(0, self.scroll - 5)
            elif key == "k":
                self.scroll += 5
            elif key == "0" and not self.job:
                return False
        elif not self.job:
            if key == "\x1b":
                self.prompt, self.entry = None, ""
            elif key == "r" and not self.prompt:
                if self.manager:
                    self.next_refresh = 0
                else:
                    self.engines, self.error = [], ""
                    self.launch("detect", "Looking for Docker or Podman", self.detect)
            elif key in ("\r", "\n"):
                value, self.entry = self.entry, ""
                if value == "0" and not self.prompt:
                    return False
                self.enter(value)
            elif key in ("\x08", "\x7f"):
                self.entry = self.entry[:-1]
            elif len(self.entry) < 3 and (key.isdecimal() or (self.prompt == "stop" and key in ("y", "n"))):
                self.entry += key
        return True


def interactive(args, backend):
    with screen() as active:
        if not active:
            return False
        with ThreadPoolExecutor(max_workers=1) as executor, backend.watch_keys(0.12) as read_key:
            dashboard = Dashboard(args, backend, executor)
            while True:
                dashboard.tick()
                dashboard.draw()
                if not dashboard.key(read_key()):
                    return True
