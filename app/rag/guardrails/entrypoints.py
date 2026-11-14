"""Contracts: docs/documents/guardrails/entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from uuid import UUID

from rag.contracts import GuardrailsResult


async def check_question(raw_question: str, revision_ids: frozenset[UUID]) -> GuardrailsResult:
    """Проверить и нормализовать вопрос до обращения к кешу и поиску.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        raw_question: Исходный текст либо сохранённый prepared_text.
        revision_ids: Редакции закреплённого поколения для примеров темы.

    Returns:
        Один вариант GuardrailsResult.
    """
    raise NotImplementedError
