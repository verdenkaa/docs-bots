"""JSON-события по спецификации docs/documents/faq/logging.md."""

import json
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import monotonic
from typing import Literal

_context = ContextVar("rag_event_context", default={})
_trace = ContextVar("rag_trace", default=None)
_parent_step = ContextVar("rag_parent_step", default=None)


def _utc_now():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


@contextmanager
def _trace_context(started, steps):
    token = _trace.set((started, steps))
    parent = _parent_step.set(None)
    try:
        yield
    finally:
        _parent_step.reset(parent)
        _trace.reset(token)


@contextmanager
def _span(name, **fields):
    started = monotonic()
    record = dict(name=name, outcome="completed", **fields)
    trace, parent = _trace.get(), _parent_step.get()
    if trace is not None:
        origin, steps = trace
        record["offset_ms"] = round((started - origin) * 1000, 3)
        (steps if parent is None else parent.setdefault("steps", [])).append(record)
    token = _parent_step.set(record)
    try:
        yield record
    except BaseException as exc:
        cancelled = isinstance(exc, asyncio.CancelledError)
        record.update(outcome="cancelled" if cancelled else "failed",
                      error_code="cancelled" if cancelled else getattr(exc, "code", type(exc).__name__))
        raise
    finally:
        record["duration_ms"] = round((monotonic() - started) * 1000, 3)
        _parent_step.reset(token)


@contextmanager
def _event_context(**fields):
    token = _context.set(fields)
    try:
        yield
    finally:
        _context.reset(token)


def emit_event(
    component: str,
    operation: str,
    event: Literal["started", "finished"],
    run_id: str,
    **fields: object,
) -> None:
    """Вывести одно JSON-событие в stdout и сразу сбросить буфер.

    Args:
        component: Владелец операции, например rag_core.
        operation: Имя операции пайплайна.
        event: started или finished.
        run_id: ID запуска операции для связи событий.
        **fields: Поля диагностики по спецификации логов; без токенов и секретов.

    Returns:
        None. Поля должны быть JSON-сериализуемыми; схему функция не валидирует.
    """
    record = {
        "schema_version": 1,
        "component": component,
        "operation": operation,
        "event": event,
        "run_id": run_id,
        **_context.get(),
        **fields,
    }
    record.setdefault("started_at" if event == "started" else "finished_at", _utc_now())
    record = {key: value for key, value in record.items() if value is not None}
    print(json.dumps(record, ensure_ascii=False), flush=True)
