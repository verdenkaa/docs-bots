"""Optional local process readiness signal for the container supervisor."""

import json
import os
from pathlib import Path


def _ready():
    name = os.environ.get("RAG_PROCESS_READY_FILE")
    if name:
        target = Path(name)
        temporary = target.with_suffix(".tmp")
        temporary.write_text(json.dumps({"pid": os.getpid()}), encoding="utf-8")
        os.replace(temporary, target)


def _clear():
    name = os.environ.get("RAG_PROCESS_READY_FILE")
    if name:
        target = Path(name)
        try:
            if json.loads(target.read_text(encoding="utf-8")).get("pid") == os.getpid():
                target.unlink(missing_ok=True)
        except (OSError, ValueError):
            pass
