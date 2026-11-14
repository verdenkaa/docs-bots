"""Owned process-tree cleanup. Children inherit the Runtime worker's group."""

import asyncio
import os
from pathlib import Path
import signal
import subprocess
import sys
from time import monotonic, sleep


def _info(pid):
    try:
        fields = (
            Path(f"/proc/{pid}/stat")
            .read_text(encoding="utf-8", errors="replace")
            .rsplit(")", 1)[1]
            .split()
        )
        return int(fields[1]), fields[0], fields[19]
    except (OSError, ValueError, IndexError):
        return None


def _tree(root, expected=None):
    result = {}
    if sys.platform != "linux":
        return result
    current = _info(root)
    if current is None or (expected is not None and current[2] != expected):
        return result
    if current is not None:
        result[root] = current[2]
    records = {}
    for entry in Path("/proc").iterdir():
        if entry.name.isdecimal() and (record := _info(int(entry.name))) is not None:
            records[int(entry.name)] = record
    changed = True
    while changed:
        changed = False
        for pid, (parent, _, birth) in records.items():
            if parent in result and pid not in result:
                result[pid] = birth
                changed = True
    return result


def _kill(root, known):
    # Stop the root before enumerating descendants so inference cannot create
    # another executor while its old tree is being retired. Birth stamps avoid
    # signalling an unrelated process after PID reuse.
    targets = dict(known)
    end = monotonic() + 3
    while monotonic() < end:
        # Freeze every discovered descendant before another scan. Stopping only
        # the parent leaves native model workers free to fork during cleanup.
        for pid, birth in targets.items():
            info = _info(pid)
            if info is not None and info[2] == birth:
                try:
                    os.kill(pid, signal.SIGSTOP)
                except ProcessLookupError:
                    pass
        discovered = {}
        for pid, birth in targets.items():
            discovered.update(_tree(pid, birth))
        if discovered.items() <= targets.items():
            break
        targets.update(discovered)
    for pid, birth in targets.items():
        info = _info(pid)
        if info is not None and info[2] == birth:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    end = monotonic() + 3
    while monotonic() < end:
        if not any(
            (info := _info(pid)) is not None and info[2] == birth and info[1] != "Z"
            for pid, birth in targets.items()
        ):
            return
        sleep(0.02)
    raise RuntimeError("Local executor did not stop")


async def _terminate(process, known):
    if sys.platform == "linux":
        await asyncio.to_thread(_kill, process.pid, known)
    elif sys.platform == "win32" and process.returncode is None:
        killer = await asyncio.create_subprocess_exec(
            "taskkill.exe",
            "/PID",
            str(process.pid),
            "/T",
            "/F",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        await killer.wait()
    elif process.returncode is None:
        process.kill()
    await process.wait()
