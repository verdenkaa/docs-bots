"""Request-local diagnostic steps; only explicit safe metadata is recorded."""
import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
from time import monotonic
from uuid import uuid4

from rag.runtime.events import emit_event
from rag.rag_api.contracts import AccessDenied, Conflict, NotFound

_current = ContextVar('rag_api_trace', default=None)
_part_fields = ContextVar('rag_api_part_fields', default={})


def _utc_now():
    return datetime.now(timezone.utc).isoformat()


class _Trace:
    def __init__(self, operation, fields):
        self.operation = operation
        self.run_id = str(uuid4())
        self.started = monotonic()
        self.started_at = _utc_now()
        self.fields = dict(fields)
        self.steps = []

    def finish(self):
        fields = {key: value for key, value in self.fields.items() if value is not None}
        emit_event('rag_api', self.operation, 'finished', self.run_id,
                   started_at=self.started_at, finished_at=_utc_now(),
                   duration_ms=round((monotonic() - self.started) * 1000),
                   steps=[{key: value for key, value in step.items() if value is not None}
                          for step in self.steps], **fields)


@contextmanager
def _operation(name, *, started=False, **fields):
    parent = _current.get()
    if parent is not None:
        yield parent
        return
    trace = _Trace(name, fields)
    token = _current.set(trace)
    if started:
        emit_event('rag_api', name, 'started', trace.run_id,
                   started_at=trace.started_at,
                   **{key: value for key, value in fields.items() if value is not None})
    try:
        yield trace
    except asyncio.CancelledError:
        trace.fields.update(outcome='cancelled', error_code='operation_cancelled')
        raise
    except BaseException:
        trace.fields.setdefault('outcome', 'error')
        trace.fields.setdefault('error_code', 'operation_failed')
        raise
    finally:
        _current.reset(token)
        trace.finish()


@contextmanager
def _step(name, *, error_code='step_failed', **fields):
    trace = _current.get()
    began = monotonic()
    step = {'name': name, 'offset_ms': round((began - trace.started) * 1000) if trace else 0,
            **_part_fields.get(), **fields}
    if trace:
        trace.steps.append(step)
    try:
        yield step
        step.setdefault('outcome', 'completed')
    except asyncio.CancelledError:
        step.update(outcome='cancelled', error_code='operation_cancelled')
        raise
    except BaseException as error:
        business = ('not_found' if isinstance(error, NotFound) else
                    'conflict' if isinstance(error, Conflict) else
                    'denied' if isinstance(error, AccessDenied) else None)
        step.update(outcome=business or 'error', error_code=business or error_code)
        raise
    finally:
        step['duration_ms'] = round((monotonic() - began) * 1000)


@contextmanager
def _part_scope(**fields):
    token = _part_fields.set({**_part_fields.get(), **fields})
    try:
        yield
    finally:
        _part_fields.reset(token)


async def _call(name, function, *args, _fields=None, _error_code='storage_error', **kwargs):
    with _step(name, error_code=_error_code, **(_fields or {})) as step:
        result = await function(*args, **kwargs)
        if type(result) is bool:
            step['outcome'] = 'recorded' if result else 'not_applied'
        elif result is None:
            step['outcome'] = 'not_required' if name == 'claim_delivery' else 'completed'
        else:
            kind = getattr(result, 'kind', None)
            step['outcome'] = kind if type(kind) is str and kind in {
                'accepted', 'duplicate', 'handled', 'busy', 'denied', 'ignored',
                'invalid_update', 'retry_later', 'sent', 'failed',
            } else 'completed'
        return result
