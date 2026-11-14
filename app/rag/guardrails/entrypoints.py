"""Contracts: docs/documents/guardrails/entrypoints.md.

Временная реализация: вопрос допускается без проверок. Полный порядок проверок —
docs/documents/guardrails/index.md.
"""


from __future__ import annotations

from uuid import UUID

from rag.contracts import AllowedQuestion, GuardrailsResult, RejectedQuestion


async def check_question(raw_question: str, revision_ids: frozenset[UUID]) -> GuardrailsResult:
    """Проверить и нормализовать вопрос до обращения к кешу и поиску.

    Временная реализация: ни инъекция, ни тема документации не проверяются, текст не меняется.
    Пустой или состоящий из пробелов вопрос получает invalid_input, остальные допускаются.

    Args:
        raw_question: Исходный текст либо сохранённый prepared_text.
        revision_ids: Редакции закреплённого поколения для примеров темы; пока не используются.

    Returns:
        Один вариант GuardrailsResult.
    """
    if not raw_question.strip():
        return RejectedQuestion(kind="invalid_input")
    return AllowedQuestion(kind="allowed", normalized_question=raw_question)
