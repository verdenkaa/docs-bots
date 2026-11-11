"""Contracts: docs/documents/retrieval/entrypoints.md."""


from uuid import UUID

from rag.contracts import SearchFragment


async def search(query: str, revision_ids: frozenset[UUID]) -> list[SearchFragment]:
    """Получить эмбеддинг, найти и отобрать фрагменты в закреплённых редакциях.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        revision_ids: Редакции из load_revision_ids для закреплённого поколения.

    Returns:
        Список SearchFragment для генератора; пустой список означает отсутствие контекста.
    """
    raise NotImplementedError
