"""Contracts: docs/documents/telegram/module/entrypoints.md."""


from typing import Literal, TypedDict

from rag.contracts import ProcessingAttempt, RagResult


class RegisteredQuestion(TypedDict):
    request_id: int
    created: bool


class RecordedUpdate(TypedDict):
    kind: Literal["accepted", "duplicate"]
    request_id: int


class UnrecordedUpdate(TypedDict):
    kind: Literal["ignored", "invalid_update", "retry_later"]


IntakeResult = RecordedUpdate | UnrecordedUpdate


class ClaimedDelivery(TypedDict):
    attempt: ProcessingAttempt
    chat_id: int
    result: RagResult


class Sent(TypedDict):
    kind: Literal["sent"]


class SendFailed(TypedDict):
    kind: Literal["failed"]
    retryable: bool
    uncertain: bool
    retry_after_seconds: int | None


SendResult = Sent | SendFailed
