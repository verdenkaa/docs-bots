"""Contracts: docs/documents/model-gateway/entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Literal

from rag.contracts import ImageContent
from rag.model_gateway.contracts import EmbeddingResult, GenerationOptions, ImageDescription, InjectionResult, ModelBinding, ModelGenerationResult, ModelMessage, ModelRole, OcrResult, RerankResult


async def start_models(bindings: Mapping[ModelRole, ModelBinding]) -> None:
    """Инициализировать выбранные провайдеры и загрузить локальные модели процесса.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        bindings: Один выбранный адаптер на каждую нужную роль. Источник — конфигурация запуска.

    Returns:
        Одно значение Python `None`.
    """
    raise NotImplementedError


async def close_models() -> None:
    """Освободить модели и клиенты провайдеров при остановке процесса.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Returns:
        Одно значение Python `None`.
    """
    raise NotImplementedError


async def embed_texts(texts: Sequence[str], purpose: Literal['query', 'document'], *, timeout_s: float) -> EmbeddingResult:
    """Получить эмбеддинги через выбранный локальный или удалённый провайдер.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        texts: Тексты из нормализованного вопроса или результата prepare_chunks; элементы непустые.
        purpose: Выбирает инструкции кодирования внутри одного профиля.
        timeout_s: Положительный конечный бюджет всего вызова в секундах.

    Returns:
        Один объект Pydantic `EmbeddingResult`.
    """
    raise NotImplementedError


async def rerank(query: str, candidates: Sequence[str], *, timeout_s: float) -> RerankResult:
    """Оценить релевантность кандидатов без бизнес-фильтрации.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query: Непустой нормализованный вопрос из Guardrails.
        candidates: Тексты fragment.text в порядке выдачи find_chunks; позиции фиксируются до вызова.
        timeout_s: Положительный конечный бюджет ожидания и inference.

    Returns:
        Один объект Pydantic `RerankResult`; элементы scores — объекты `RerankScore`.
    """
    raise NotImplementedError


async def classify_injection(text: str, *, timeout_s: float) -> InjectionResult:
    """Получить наблюдение модели защиты, не принимая бизнес-решение за Guardrails.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        text: Непустой исходный вопрос пользователя, без преобразований Guardrails.
        timeout_s: Положительный конечный бюджет проверки.

    Returns:
        Один объект Pydantic `InjectionResult`; это наблюдение детектора, не решение Guardrails.
    """
    raise NotImplementedError


async def generate(messages: Sequence[ModelMessage], options: GenerationOptions, *, timeout_s: float) -> ModelGenerationResult:
    """Вызвать генерацию и собрать поток провайдера в один результат.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        messages: Непустой список сообщений: системные правила и вопрос с контекстом, роли сохранены.
        options: Бюджет вывода и выбранная настройка генерации из конфигурации.
        timeout_s: Положительный конечный бюджет до полного ответа, включая stream.

    Returns:
        Один объект Pydantic `ModelGenerationResult`, не генератор токенов.
    """
    raise NotImplementedError


async def recognize_text(image: ImageContent, *, timeout_s: float) -> OcrResult:
    """Извлечь только текст изображения; пустой OCR допустим.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        image: Байты и проверенный MIME из Runtime.
        timeout_s: Весь бюджет, включая ожидание исполнителя.

    Returns:
        OcrResult.
    """
    raise NotImplementedError


async def describe_image(image: ImageContent, *, timeout_s: float) -> ImageDescription:
    """Получить VLM-описание изображения документа для Document Converter.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        image: Извлечённое изображение документа.
        timeout_s: Бюджет Gateway.

    Returns:
        ImageDescription.
    """
    raise NotImplementedError
