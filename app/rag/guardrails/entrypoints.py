"""Contracts: docs/documents/guardrails/entrypoints.md.

Временная реализация: детектор инъекций вызывается через Model Gateway, но его ответ не влияет на
решение, вопрос всегда допускается. Полный порядок проверок — docs/documents/guardrails/index.md.
"""


from __future__ import annotations

from uuid import UUID

from rag.contracts import AllowedQuestion, DetectorUnavailable, GuardrailsResult, RejectedQuestion, StepError
from rag.model_gateway import entrypoints as model_gateway

# бюджет вызова детектора; в документации не задан (конфигурация Runtime), поэтому пока константа — предположение
_DETECTOR_TIMEOUT_S = 5.0


async def check_question(raw_question: str, revision_ids: frozenset[UUID]) -> GuardrailsResult:
    """Проверить и нормализовать вопрос до обращения к кешу и поиску.

    Временная реализация: вопрос проверяется через classify_injection, но ответ детектора игнорируется,
    порог и правило интерпретации не применяются, текст не меняется. Тема документации не проверяется.
    Пустой вопрос получает invalid_input; сбой детектора — detector_unavailable, как в контракте.

    Args:
        raw_question: Исходный текст либо сохранённый prepared_text.
        revision_ids: Редакции закреплённого поколения для примеров темы; пока не используются.

    Returns:
        Один вариант GuardrailsResult.
    """
    if not raw_question.strip():
        return RejectedQuestion(kind="invalid_input")
    try:
        await model_gateway.classify_injection(raw_question, timeout_s=_DETECTOR_TIMEOUT_S)
    except StepError as error:
        return DetectorUnavailable(kind="detector_unavailable", retryable=error.retryable)
    return AllowedQuestion(kind="allowed", normalized_question=raw_question)
