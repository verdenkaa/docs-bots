"""Contracts: docs/documents/rag-core/answer-generator-entrypoints.md."""


from collections.abc import Sequence

from rag.contracts import GenerationResult, SearchFragment


async def generate_answer(query: str, fragments: Sequence[SearchFragment]) -> GenerationResult:
    """Сформировать ответ по найденным фрагментам через Model Gateway и проверить цитаты.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        fragments: Упорядоченные фрагменты с текстом и метаданными для контекста.

    Returns:
        GenerationResult: answered с ID цитат либо insufficient_evidence.
    """
    raise NotImplementedError
