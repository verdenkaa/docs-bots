"""Attempt budgets, sanitized boundary validation and structured tracing."""

from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from rag.contracts import StepError
from rag.dto import DTO
from rag.ingestion._dependencies import _Dependencies


class _AttemptExpired(Exception):
    """No new operation may start with this attempt's remaining budget."""


class _DatabaseUnknown(Exception):
    """An operation's database state or commit cannot be established."""


class _LostAttempt(Exception):
    """PostgreSQL confirmed that this consumer no longer owns the work."""


class _Boundary:
    @staticmethod
    def _plain(value: Any) -> Any:
        # model_validate(existing_model) does not revalidate mutated instances.
        if isinstance(value, DTO):
            return value.model_dump(warnings=False)
        if isinstance(value, list):
            return [_Boundary._plain(item) for item in value]
        return value

    @staticmethod
    def validate(value: Any, annotation: Any, error_code: str) -> Any:
        try:
            return TypeAdapter(annotation).validate_python(
                _Boundary._plain(value), strict=True,
            )
        except ValidationError:
            # ValidationError may include private document/model input.
            raise StepError(error_code, False) from None


class _Budget:
    def __init__(self, expires: float, deps: _Dependencies) -> None:
        self._expires = expires
        self._deps = deps

    @classmethod
    def from_deadline(cls, deadline: datetime, deps: _Dependencies) -> _Budget:
        remaining = (deadline - deps.now()).total_seconds()
        return cls(deps.monotonic() + max(remaining, 0.0), deps)

    @classmethod
    def for_seconds(cls, seconds: float, deps: _Dependencies) -> _Budget:
        return cls(deps.monotonic() + seconds, deps)

    def remaining(self, step_limit: float) -> float:
        remaining = self._expires - self._deps.monotonic()
        if remaining <= 0:
            raise _AttemptExpired
        return min(remaining, step_limit)


class _Trace:
    def __init__(self, job_id: str, deps: _Dependencies) -> None:
        self.deps = deps
        self.run_id = str(uuid4())
        self.started = deps.monotonic()
        self.started_at = deps.now().isoformat()
        self.fields: dict[str, Any] = {"job_id": job_id, "revision_id": job_id}
        self.steps: list[dict[str, Any]] = []
        self.deps.emit("ingestion_worker", "handle_ingestion_job", "started",
                       self.run_id, started_at=self.started_at, **self.fields)

    async def call(self, name: str, budget: _Budget, limit: float,
                   function: Any, *args: Any, **kwargs: Any) -> Any:
        seconds = budget.remaining(limit)
        started = self.deps.monotonic()
        step: dict[str, Any] = {
            "name": name,
            "offset_ms": round((started - self.started) * 1000),
            "outcome": "error",
        }
        self.steps.append(step)
        try:
            async with asyncio.timeout(seconds):
                result = await function(*args, **kwargs)
            step["outcome"] = "completed"
            return result
        except StepError as error:
            step["error_code"] = error.code
            raise
        except TimeoutError:
            step["error_code"] = "step_timeout"
            raise
        except asyncio.CancelledError:
            step["outcome"] = "cancelled"
            raise
        finally:
            step["duration_ms"] = round((self.deps.monotonic() - started) * 1000)

    def finish(self, outcome: str, decision: str | None,
               error_code: str | None = None) -> None:
        fields = dict(self.fields)
        if decision is not None:
            fields["consume_decision"] = decision
        if error_code is not None:
            fields["error_code"] = error_code
        self.deps.emit(
            "ingestion_worker", "handle_ingestion_job", "finished", self.run_id,
            started_at=self.started_at, finished_at=self.deps.now().isoformat(),
            duration_ms=round((self.deps.monotonic() - self.started) * 1000),
            outcome=outcome, steps=self.steps, **fields,
        )
