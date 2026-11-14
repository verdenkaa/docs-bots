"""Contracts: docs/documents/rag-core/entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from rag.contracts import ConsumeDecision, RequestRef


async def handle_rag_question(event: RequestRef) -> ConsumeDecision:
    """Оркестрировать текст/OCR, Guardrails, кеш, поиск, генерацию и сохранение QuestionOutcome.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        event: Вход по контракту владельца.

    Returns:
        Одна строка ConsumeDecision.
    """
    raise NotImplementedError
