"""Standalone coordination. Runtime supplies durable coordination in production."""

from collections import OrderedDict
from time import monotonic
from uuid import uuid4

from rag.contracts import StepError
from rag.model_gateway.providers import CallCoordinator


class _MemoryCoordinator(CallCoordinator):
    def __init__(self, settings):
        self.settings, self.records = settings, OrderedDict()

    async def acquire(self, key, profile_id, role, operation):
        record = self.records.get(key)
        if (
            record is not None
            and record["kind"] == "done"
            and record["expires"] <= monotonic()
        ):
            del self.records[key]
            record = None
        if record is not None:
            return (
                dict(record) if record["kind"] == "done" else {"kind": record["kind"]}
            )
        token = uuid4().hex
        self.records[key] = dict(kind="busy", token=token)
        return dict(kind="owned", token=token)

    async def complete(self, key, token, value):
        record = self.records.get(key)
        if record is None or record.get("token") != token:
            raise StepError("model_unavailable", True)
        self.records[key] = dict(
            kind="done", value=value, expires=monotonic() + self.settings.cache_ttl_s
        )
        completed = [
            name for name, item in self.records.items() if item["kind"] == "done"
        ]
        for name in (
            completed[: -self.settings.cache_size]
            if self.settings.cache_size
            else completed
        ):
            del self.records[name]

    async def release(self, key, token, *, safe):
        record = self.records.get(key)
        if record is not None and record.get("token") == token:
            if safe:
                del self.records[key]
            else:
                record["kind"] = "uncertain"
