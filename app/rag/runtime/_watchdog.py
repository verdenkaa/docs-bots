"""Worker child protocol. A separate parent enforces these monotonic budgets."""

import json
import os
from contextlib import contextmanager
from datetime import UTC, datetime
from time import monotonic


_deadline = None
_identity = {}


def _send(budget, step):
    descriptor = os.environ.get("RAG_WATCHDOG_FD")
    if descriptor is not None:
        payload = json.dumps({"budget": budget, "step": step, **_identity}).encode() + b"\n"
        os.write(int(descriptor), payload)


def _begin(seconds, job_id):
    global _deadline, _identity
    _identity = {"job_id": str(job_id)}
    _deadline = monotonic() + seconds
    _send(seconds, "claim")


def _claimed(attempt):
    global _deadline, _identity
    # Only the ingestion consumer arms a watchdog. Direct adapter calls do not.
    if _deadline is not None:
        remaining = max(0, (attempt.deadline_at - datetime.now(UTC)).total_seconds())
        _deadline = monotonic() + remaining
        _identity = {"job_id": str(attempt.job_id), "attempt_no": attempt.attempt_no}
        _send(remaining, "attempt")


def _end():
    global _deadline, _identity
    _deadline = None
    _send(None, "idle")
    _identity = {}


@contextmanager
def _step(seconds, name):
    if _deadline is not None:
        _send(max(0, min(seconds, _deadline - monotonic())), name)
    try:
        yield
    finally:
        if _deadline is not None:
            _send(max(0, _deadline - monotonic()), "attempt")
