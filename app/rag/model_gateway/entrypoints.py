"""Contracts: docs/documents/model-gateway/entrypoints.md.

Scaffold only: no model downloads, API requests or global model state yet.
Runtime initializes one selected binding per role in each owning process.
"""

from collections.abc import Mapping, Sequence
from typing import Literal

from .contracts import EmbeddingResult, GenerationOptions, InjectionResult, ModelBinding, ModelGenerationResult, ModelMessage, ModelRole, RerankResult


async def start_models(bindings: Mapping[ModelRole, ModelBinding]) -> None:
    """Инициализировать выбранные провайдеры и загрузить локальные модели процесса.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        bindings: Один выбранный адаптер на каждую нужную роль. Источник — конфигурация запуска.

    Returns:
        None после успешной инициализации. Вызывает Runtime до бизнес-вызовов.

    Raises:
        StepError: Провайдер недоступен, нарушен формат или исчерпан бюджет.

    Отмена ожидания не гарантирует остановку локального вычисления или удалённого вызова.
    """
    raise NotImplementedError


async def close_models() -> None:
    """Освободить модели и клиенты провайдеров при остановке процесса.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Returns:
        None после завершения освобождения ресурсов.

    Raises:
        StepError: Провайдер недоступен, нарушен формат или исчерпан бюджет.

    Отмена ожидания не гарантирует остановку локального вычисления или удалённого вызова.
    """
    raise NotImplementedError


async def embed_texts(
    texts: Sequence[str],
    purpose: Literal["query", "document"],
    *,
    timeout_s: float,
) -> EmbeddingResult:
    """Получить эмбеддинги через выбранный локальный или удалённый провайдер.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        texts: Тексты из нормализованного вопроса или результата prepare_chunks; элементы непустые.
        purpose: Выбирает инструкции кодирования внутри одного профиля.
        timeout_s: Положительный конечный бюджет всего вызова в секундах.

    Returns:
        EmbeddingResult: профиль и векторы строго в порядке texts.
        Профиль и размерность должны совпадать у индексации и поиска.

    Raises:
        StepError: Провайдер недоступен, нарушен формат или исчерпан бюджет.

    Отмена ожидания не гарантирует остановку локального вычисления или удалённого вызова.
    """
    raise NotImplementedError


async def rerank(
    query: str, candidates: Sequence[str], *, timeout_s: float
) -> RerankResult:
    """Оценить релевантность кандидатов без бизнес-фильтрации.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        query: Непустой нормализованный вопрос из Guardrails.
        candidates: Тексты fragment.text в порядке выдачи find_chunks; позиции фиксируются до вызова.
        timeout_s: Положительный конечный бюджет ожидания и inference.

    Returns:
        RerankResult с индексами исходных candidates и оценками; сортирует Searcher.

    Raises:
        StepError: Провайдер недоступен, нарушен формат или исчерпан бюджет.

    Отмена ожидания не гарантирует остановку локального вычисления или удалённого вызова.
    """
    raise NotImplementedError


async def classify_injection(text: str, *, timeout_s: float) -> InjectionResult:
    """Получить наблюдение модели защиты, не принимая бизнес-решение за Guardrails.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        text: Непустой исходный вопрос пользователя, без преобразований Guardrails.
        timeout_s: Положительный конечный бюджет проверки.

    Returns:
        InjectionResult с label и/или score; score не обязательно вероятность.

    Raises:
        StepError: Провайдер недоступен, нарушен формат или исчерпан бюджет.

    Отмена ожидания не гарантирует остановку локального вычисления или удалённого вызова.
    """
    raise NotImplementedError


async def generate(
    messages: Sequence[ModelMessage], options: GenerationOptions, *, timeout_s: float
) -> ModelGenerationResult:
    """Вызвать генерацию и собрать поток провайдера в один результат.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        messages: Непустой список сообщений: системные правила и вопрос с контекстом, роли сохранены.
        options: Бюджет вывода и выбранная настройка генерации из конфигурации.
        timeout_s: Положительный конечный бюджет до полного ответа, включая stream.

    Returns:
        ModelGenerationResult с текстом, причиной остановки и доступным usage.
        Проверку достаточности оснований и цитат выполняет Answer Generator.

    Raises:
        StepError: Провайдер недоступен, нарушен формат или исчерпан бюджет.

    Отмена ожидания не гарантирует остановку локального вычисления или удалённого вызова.
    """
    raise NotImplementedError
