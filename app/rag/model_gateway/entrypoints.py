"""Contracts: docs/documents/model-gateway/entrypoints.md.

Provider-independent execution; model SDKs and weights are supplied by adapters.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from rag.contracts import ImageContent
from rag.model_gateway.contracts import (
    EmbeddingResult,
    GenerationOptions,
    ImageDescription,
    InjectionResult,
    ModelBinding,
    ModelGenerationResult,
    ModelMessage,
    ModelRole,
    OcrResult,
    RerankResult,
)
from rag.model_gateway import _manager


async def start_models(bindings: Mapping[ModelRole, ModelBinding]) -> None:
    """Инициализировать выбранные провайдеры и загрузить локальные модели процесса.

    Args:
        bindings: Один выбранный адаптер на каждую нужную роль. Источник — конфигурация запуска.

    Returns:
        Одно значение Python `None`.
    """
    await _manager._start(bindings)


async def close_models() -> None:
    """Освободить модели и клиенты провайдеров при остановке процесса.

    Returns:
        Одно значение Python `None`.
    """
    await _manager._close()


async def embed_texts(
    texts: Sequence[str], purpose: Literal["query", "document"], *, timeout_s: float
) -> EmbeddingResult:
    """Получить эмбеддинги через выбранный локальный или удалённый провайдер.

    Args:
        texts: Тексты из нормализованного вопроса или результата prepare_chunks; элементы непустые.
        purpose: Выбирает инструкции кодирования внутри одного профиля.
        timeout_s: Положительный конечный бюджет всего вызова в секундах.

    Returns:
        Один объект Pydantic `EmbeddingResult`.
    """
    return await _manager._call(
        "embedding", "embed_texts", dict(texts=texts, purpose=purpose), timeout_s
    )


async def rerank(
    query: str, candidates: Sequence[str], *, timeout_s: float
) -> RerankResult:
    """Оценить релевантность кандидатов без бизнес-фильтрации.

    Args:
        query: Непустой нормализованный вопрос из Guardrails.
        candidates: Тексты fragment.text в порядке выдачи find_chunks; позиции фиксируются до вызова.
        timeout_s: Положительный конечный бюджет ожидания и inference.

    Returns:
        Один объект Pydantic `RerankResult`; элементы scores — объекты `RerankScore`.
    """
    return await _manager._call(
        "reranker", "rerank", dict(query=query, candidates=candidates), timeout_s
    )


async def classify_injection(text: str, *, timeout_s: float) -> InjectionResult:
    """Получить наблюдение модели защиты, не принимая бизнес-решение за Guardrails.

    Args:
        text: Непустой исходный вопрос пользователя, без преобразований Guardrails.
        timeout_s: Положительный конечный бюджет проверки.

    Returns:
        Один объект Pydantic `InjectionResult`; это наблюдение детектора, не решение Guardrails.
    """
    # ВРЕМЕННО по договорённости с командой: детектор пока не подключён, всегда «всё нормально».
    # Настоящий вызов:
    #   return await _manager._call("injection", "classify_injection", dict(text=text), timeout_s)
    return InjectionResult(profile_id="stub", label="benign", score=None)


async def generate(
    messages: Sequence[ModelMessage], options: GenerationOptions, *, timeout_s: float
) -> ModelGenerationResult:
    """Вызвать генерацию и собрать поток провайдера в один результат.

    Args:
        messages: Непустой список сообщений: системные правила и вопрос с контекстом, роли сохранены.
        options: Бюджет вывода и выбранная настройка генерации из конфигурации.
        timeout_s: Положительный конечный бюджет до полного ответа, включая stream.

    Returns:
        Один объект Pydantic `ModelGenerationResult`, не генератор токенов.
    """
    return await _manager._call(
        "generation", "generate", dict(messages=messages, options=options), timeout_s
    )


async def recognize_text(image: ImageContent, *, timeout_s: float) -> OcrResult:
    """Извлечь только текст изображения; пустой OCR допустим.

    Args:
        image: Байты и проверенный MIME из Runtime.
        timeout_s: Весь бюджет, включая ожидание исполнителя.

    Returns:
        OcrResult.
    """
    return await _manager._call("ocr", "recognize_text", dict(image=image), timeout_s)


async def describe_image(image: ImageContent, *, timeout_s: float) -> ImageDescription:
    """Получить VLM-описание изображения документа для Document Converter.

    Args:
        image: Извлечённое изображение документа.
        timeout_s: Бюджет Gateway.

    Returns:
        ImageDescription.
    """
    return await _manager._call(
        "vision", "describe_image", dict(image=image), timeout_s
    )
