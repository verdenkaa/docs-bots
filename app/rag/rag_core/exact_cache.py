"""Contracts: docs/documents/rag-core/exact-cache-entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from rag.contracts import RagResult


async def get_answer(query: str, corpus_generation: int) -> RagResult | None:
    """Найти точный ответ и сверить его поколение с закреплённым за запросом.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        corpus_generation: Поколение, закреплённое в PostgreSQL за этим запросом.

    Returns:
        Объект Pydantic `RagResult` или `None` при промахе, несовпадении поколения или сбое Redis.
    """
    raise NotImplementedError


async def store_answer(query: str, corpus_generation: int, result: RagResult) -> None:
    """Сохранить точный ответ с поколением, не затирая более новую запись.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        corpus_generation: Поколение, закреплённое в PostgreSQL за этим запросом.
        result: Текст ответа и непустой список источников, уже сохранённые в PostgreSQL.

    Returns:
        Одно значение Python, `None`.
    """
    raise NotImplementedError
