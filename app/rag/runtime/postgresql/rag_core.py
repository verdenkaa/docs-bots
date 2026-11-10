"""Contracts: docs/documents/infrastructure/postgresql/rag-core.md."""


from datetime import datetime
from uuid import UUID

from rag.contracts import ClaimedQuestion, ProcessingAttempt, RagResult


async def claim_question(request_id: int) -> ClaimedQuestion | None:
    raise NotImplementedError


async def load_revision_ids(corpus_generation: int) -> frozenset[UUID]:
    raise NotImplementedError


async def complete_question(attempt: ProcessingAttempt, result: RagResult) -> bool:
    raise NotImplementedError


async def schedule_question_retry(attempt: ProcessingAttempt, retry_at: datetime) -> bool:
    raise NotImplementedError
