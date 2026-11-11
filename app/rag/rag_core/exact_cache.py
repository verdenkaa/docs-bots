"""Contracts: docs/documents/rag-core/exact-cache-entrypoints.md."""


from rag.contracts import RagResult


async def get_answer(query: str, corpus_generation: int) -> RagResult | None:
    """Найти точный ответ и сверить его поколение с закреплённым за запросом.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        corpus_generation: Поколение, закреплённое в PostgreSQL за этим запросом.

    Returns:
        RagResult при совпадении, None при промахе, устаревании или сбое кеша.
        Устаревшее значение удаляется условно, без удаления новой конкурентной записи.
    """
    raise NotImplementedError


async def store_answer(query: str, corpus_generation: int, result: RagResult) -> None:
    """Сохранить точный ответ с поколением, не затирая более новую запись.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        corpus_generation: Поколение, закреплённое в PostgreSQL за этим запросом.
        result: Текст ответа и непустой список источников, уже сохранённые в PostgreSQL.

    Returns:
        None; сбой Redis не должен срывать готовый ответ.
    """
    raise NotImplementedError
