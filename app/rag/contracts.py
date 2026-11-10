"""Contracts: docs/documents/rag-core/contracts.md."""


from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID


class RequestRef(TypedDict):
    request_id: int


ConsumeDecision = Literal["ack", "retry_broker"]


class ProcessingAttempt(TypedDict):
    request_id: int
    attempt_no: int
    deadline_at: datetime


class ClaimedQuestion(TypedDict):
    attempt: ProcessingAttempt
    raw_question: str
    corpus_generation: int


class SourceLocator(TypedDict, total=False):
    page: int
    section: str


class SourceRef(TypedDict):
    chunk_id: UUID
    document_id: UUID
    revision_id: UUID
    source_name: str
    locator: SourceLocator | None
    source_url: str | None


class SearchFragment(SourceRef):
    text: str


class RagResult(TypedDict):
    answer_text: str
    sources: list[SourceRef]


class AllowedQuestion(TypedDict):
    kind: Literal["allowed"]
    normalized_question: str


class RejectedQuestion(TypedDict):
    kind: Literal["blocked", "invalid_input"]


class DetectorUnavailable(TypedDict):
    kind: Literal["detector_unavailable"]
    retryable: bool


GuardrailsResult = AllowedQuestion | RejectedQuestion | DetectorUnavailable


class GeneratedAnswer(TypedDict):
    kind: Literal["answered"]
    answer_text: str
    cited_chunk_ids: list[UUID]


class InsufficientEvidence(TypedDict):
    kind: Literal["insufficient_evidence"]


GenerationResult = GeneratedAnswer | InsufficientEvidence


class StepError(Exception):
    def __init__(self, code: str, retryable: bool) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class StorageError(Exception):
    """A storage operation could not be confirmed."""
