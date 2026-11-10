"""Contracts: docs/documents/infrastructure/postgresql/rag-api.md."""


from datetime import datetime

from rag.contracts import ProcessingAttempt
from rag.rag_api.contracts import ClaimedDelivery, RegisteredQuestion


async def register_question(update_id: int, chat_id: int, question_text: str) -> RegisteredQuestion | None:
    raise NotImplementedError


async def claim_delivery(request_id: int) -> ClaimedDelivery | None:
    raise NotImplementedError


async def schedule_delivery_retry(attempt: ProcessingAttempt, retry_at: datetime, uncertain: bool) -> bool:
    raise NotImplementedError


async def complete_delivery(attempt: ProcessingAttempt, delivered: bool, uncertain: bool) -> bool:
    raise NotImplementedError
