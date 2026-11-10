"""Contracts: docs/documents/telegram/module/entrypoints.md."""


from collections.abc import Mapping

from rag.contracts import ConsumeDecision, RagResult, RequestRef
from rag.rag_api.contracts import IntakeResult, SendResult


async def handle_telegram_update(update: Mapping[str, object]) -> IntakeResult:
    raise NotImplementedError


async def handle_rag_result(event: RequestRef) -> ConsumeDecision:
    raise NotImplementedError


async def send_answer(chat_id: int, result: RagResult) -> SendResult:
    raise NotImplementedError
