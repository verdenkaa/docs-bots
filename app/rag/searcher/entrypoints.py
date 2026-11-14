"""Contracts: docs/documents/retrieval/entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from uuid import UUID

from rag.contracts import SearchFragment


async def search(query: str, revision_ids: frozenset[UUID]) -> list[SearchFragment]:
    """Получить эмбеддинг, найти и отобрать фрагменты в закреплённых редакциях.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        revision_ids: Редакции из load_revision_ids для закреплённого поколения.

    Returns:
        Список объектов Pydantic `SearchFragment`, отсортированных по релевантности. `[]` означает, что кандидатов нет. `[]` в колонке поля обозначает элемент списка.
    """
    raise NotImplementedError
