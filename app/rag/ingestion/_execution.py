"""Attempt budgets and sanitized boundary validation."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import TypeAdapter, ValidationError

from rag.contracts import StepError
from rag.dto import DTO
from rag.ingestion._dependencies import _Dependencies


class _AttemptExpired(Exception):
    """No new operation may start with this attempt's remaining budget."""
    code = "attempt_expired"


class _DatabaseUnknown(Exception):
    """An operation's database state or commit cannot be established."""
    code = "storage_unconfirmed"


class _LostAttempt(Exception):
    """PostgreSQL confirmed that this consumer no longer owns the work."""
    code = "attempt_lost"


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
