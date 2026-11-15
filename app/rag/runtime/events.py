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

# Explicit diagnostic metadata only. Never flatten arbitrary input or result values.
_STEP_FIELDS = frozenset({
    "duration_ms", "offset_ms", "outcome", "error_code", "model_role", "profile_id",
    "execution", "wait_ms", "inference_ms", "reused", "part_no", "purpose",
    "request_id", "case_id", "delivery_id", "variant", "delivery_variant", "http_status", "status_code",
    "retry_after_seconds", "retryable", "uncertain", "delivered", "telegram_operation",
    "telegram_error_code", "method", "status", "chunk_count", "point_count", "postprocess_ms",
})
_STEP_CONTEXT = frozenset({
    "schema_version", "component", "operation", "event", "run_id", "request_id",
    "delivery_id", "case_id", "job_id", "revision_id", "update_id", "message_id",
    "route", "attempt_no", "received_at", "started_at", "finished_at", "purpose",
    "document_id", "document_version", "corpus_generation",
})


def _step_events(record, steps, parent_path="", depth=0):
    """Project existing timing records into queryable logs, retaining their hierarchy."""
    for index, step in enumerate(steps):
        name = step["name"]
        path = f"{parent_path}/{index}:{name}"
        projected = {key: value for key, value in record.items() if key in _STEP_CONTEXT}
        projected.update({key: value for key, value in step.items() if key in _STEP_FIELDS})
        projected.update(operation="step", parent_operation=record["operation"],
                         step_name=name, step_path=path, step_depth=step.get("step_depth", depth))
        if step.get("parent_step"):
            projected["parent_step"] = step["parent_step"]
        elif parent_path:
            projected["parent_step"] = parent_path.rsplit(":", 1)[-1]
        print(json.dumps(projected, ensure_ascii=False), flush=True)
        _step_events(record, step.get("steps", []), path, depth + 1)


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
    if event == "finished" and record.get("steps"):
        _step_events(record, record["steps"])
    print(json.dumps(record, ensure_ascii=False), flush=True)
