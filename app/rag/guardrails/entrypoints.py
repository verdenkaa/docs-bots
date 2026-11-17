"""Contracts: docs/documents/guardrails/entrypoints.md.

Guardrails: ввод и бессмыслица, короткие атаки, инъекция через Model Gateway, нормализация, тема документации.
Порядок и границы: docs/documents/guardrails/index.md. Параметры — rag/guardrails/_config.py.
"""


from __future__ import annotations

import asyncio
from uuid import UUID

from rag.contracts import (
    AllowedQuestion, DetectorUnavailable, GuardrailsResult, OutOfScope, RejectedQuestion, StepError,
)
from rag.guardrails._checks import _is_injection, _is_nonsense, _is_short_attack, _normalize
from rag.guardrails._config import guardrails_settings
from rag.model_gateway import entrypoints as model_gateway
from rag.runtime import qdrant


async def check_question(raw_question: str, revision_ids: frozenset[UUID]) -> GuardrailsResult:
    """Проверить и нормализовать вопрос до обращения к кешу и поиску.

    Args:
        raw_question: Исходный текст либо сохранённый prepared_text.
        revision_ids: Редакции закреплённого поколения для примеров темы.

    Returns:
        Один вариант GuardrailsResult.
    """
    settings = guardrails_settings()
    if (not raw_question.strip() or len(raw_question) > settings.max_question_chars
            or _is_nonsense(raw_question)):
        return RejectedQuestion(kind="invalid_input")
    if _is_short_attack(raw_question):
        # модель-детектор пропускает около половины коротких атак; это правило от неё не зависит
        return RejectedQuestion(kind="blocked")

    try:
        normalized = _normalize(raw_question)
        if not normalized:
            return RejectedQuestion(kind="invalid_input")
        # детектор получает исходный текст без преобразований. Если нормализация убрала невидимые символы,
        # дальше пойдёт другой текст — он тоже проверяется, иначе инъекцию можно спрятать от детектора
        checked = [raw_question] if normalized == " ".join(raw_question.split()) else [raw_question, normalized]
        for text in checked:
            observation = await model_gateway.classify_injection(text, timeout_s=settings.detector_timeout_s)
            injection = _is_injection(observation, settings.injection_score_thresholds)
            if injection is None:
                # score без правила интерпретации для профиля: молча допускать нельзя
                return DetectorUnavailable(kind="detector_unavailable", retryable=False)
            if injection:
                return RejectedQuestion(kind="blocked")
        if not revision_ids:
            # корпус пуст: тему сравнивать не с чем, Core выберет no_evidence
            return AllowedQuestion(kind="allowed", normalized_question=normalized)

        embedded = await model_gateway.embed_texts([normalized], "query", timeout_s=settings.embedding_timeout_s)
        async with asyncio.timeout(settings.topic_timeout_s):
            hits = await qdrant.find_topic_matches(embedded.vectors[0], revision_ids, 1)
    except (StepError, TimeoutError) as error:
        return DetectorUnavailable(kind="detector_unavailable", retryable=getattr(error, "retryable", True))

    if not hits:
        # при непустых revision_ids пустая выдача означает неполный индекс примеров, а не отсутствие темы
        return DetectorUnavailable(kind="detector_unavailable", retryable=False)
    if max(hit.score for hit in hits) < settings.topic_threshold:
        return OutOfScope(kind="out_of_scope")
    return AllowedQuestion(kind="allowed", normalized_question=normalized)
