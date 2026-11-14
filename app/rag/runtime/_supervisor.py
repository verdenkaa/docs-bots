"""Linux worker process-group supervision; no process is started on import."""

import json
import os
from pathlib import Path
import select
import signal
import subprocess
import sys
from time import monotonic, sleep
from uuid import uuid4

from rag.runtime.events import emit_event


def _group_alive(group):
    # Zombies cannot execute writes. Ignore them while the container's init reaps.
    for entry in Path("/proc").iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            fields = (entry / "stat").read_text(encoding="utf-8", errors="replace").rsplit(")", 1)[1].split()
            if int(fields[2]) == group and fields[0] != "Z":
                return True
        except (OSError, ValueError, IndexError):
            continue
    return False


def _stop_group(process, grace):
    for sig, duration in ((signal.SIGTERM, grace), (signal.SIGKILL, grace)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        end = monotonic() + duration
        while monotonic() < end:
            process.poll()
            if not _group_alive(process.pid):
                process.wait(timeout=grace)
                return
            sleep(0.05)
    raise RuntimeError("Worker process group did not stop; restart is forbidden")


def _supervise(command, stop, *, startup_timeout, shutdown_timeout, restart_delay=1):
    if sys.platform != "linux":
        raise RuntimeError("Worker supervision requires a Linux container")
    while not stop.is_set():
        reader, writer = os.pipe()
        os.set_blocking(reader, False)
        environment = dict(os.environ, RAG_WATCHDOG_FD=str(writer))
        try:
            process = subprocess.Popen(command, env=environment, pass_fds=(writer,), start_new_session=True)
        except BaseException:
            os.close(reader)
            raise
        finally:
            os.close(writer)
        deadline = monotonic() + startup_timeout
        pending = b""
        identity = {}
        reason = "process_exit"
        try:
            while process.poll() is None and not stop.is_set():
                if deadline is not None and monotonic() >= deadline:
                    reason = "deadline_exceeded"
                    break
                ready, _, _ = select.select([reader], [], [], 0.1)
                if ready:
                    chunk = os.read(reader, 65536)
                    if not chunk:
                        break
                    pending += chunk
                    if len(pending) > 65536:
                        reason = "invalid_watchdog_protocol"
                        break
                    while b"\n" in pending:
                        line, pending = pending.split(b"\n", 1)
                        message = json.loads(line)
                        budget = message["budget"]
                        if budget is not None and (type(budget) not in (int, float) or not 0 <= budget < 86400):
                            raise ValueError("Invalid watchdog budget")
                        deadline = None if budget is None else monotonic() + budget
                        identity = {key: message[key] for key in ("job_id", "attempt_no", "step") if key in message}
        finally:
            os.close(reader)
            _stop_group(process, shutdown_timeout)
        emit_event("ingestion_worker", "supervisor", "finished", str(uuid4()),
                   outcome="stopped" if stop.is_set() else reason, **identity)
        if not stop.is_set():
            stop.wait(restart_delay)
