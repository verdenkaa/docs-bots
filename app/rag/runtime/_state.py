"""Explicit per-process resources; no clients or services are created at import."""

import asyncio


class _Resources:
    def __init__(self, settings):
        self.settings = settings
        self.engine = None
        self.redis = None
        self.http = None
        self.qdrant = None
        self.broker = None
        self.channel = None
        self.broker_lock = asyncio.Lock()


_current = None
_lifecycle_lock = asyncio.Lock()


def _get():
    if _current is None:
        raise RuntimeError("Runtime has not been started in this process")
    return _current


def _set(value):
    global _current
    _current = value
