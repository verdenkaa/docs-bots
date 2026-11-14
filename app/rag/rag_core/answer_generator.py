"""Contracts: docs/documents/rag-core/answer-generator-entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from collections.abc import Sequence

from rag.contracts import GenerationResult, SearchFragment


async def generate_answer(query: str, fragments: Sequence[SearchFragment]) -> GenerationResult:
    """Сформировать ответ по найденным фрагментам через Model Gateway и проверить цитаты.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        fragments: Упорядоченные фрагменты с текстом и метаданными для контекста.

    Returns:
        Один объект Pydantic. `GenerationResult` объединяет два варианта по полю `kind`.
    """
    raise NotImplementedError
