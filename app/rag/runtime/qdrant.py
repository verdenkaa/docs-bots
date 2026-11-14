"""Contracts: docs/documents/infrastructure/qdrant/contracts.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from collections.abc import Sequence
from uuid import UUID

from pydantic import Field

from rag.contracts import SearchFragment
from rag.dto import DTO, NonEmptyText, NonNegativeInt


class ChunkPoint(SearchFragment):
    """Точка Qdrant: текст, метаданные, номер чанка и готовый плотный вектор."""
    chunk_no: NonNegativeInt = Field(description='Порядковый номер чанка с 0 внутри редакции; участвует в детерминированном ID.')
    dense: list[float] = Field(description='Готовый плотный вектор общего embedding-профиля индекса и поиска.')


class QdrantHit(DTO):
    """Кандидат Qdrant до окончательного отбора и реранкинга Searcher."""
    fragment: SearchFragment = Field(description='Текст кандидата и метаданные его источника.')
    score: float = Field(description='Оценка модели или поиска; шкала определяется конкретным профилем.')


class TopicHit(DTO):
    """Косинусное сходство с примером темы для решения Guardrails."""
    revision_id: UUID = Field(description='UUID конкретной редакции, совпадающий с job_id индексации.')
    score: float = Field(description='Оценка поиска или модели; шкала определяется конкретным профилем.')


class TopicPoint(DTO):
    """Пример вопроса одной редакции; не является источником ответа."""
    point_id: UUID = Field(description='Детерминированный UUIDv5 редакции и question:<номер>.')
    revision_id: UUID = Field(description='UUID конкретной редакции, совпадающий с job_id индексации.')
    question_no: NonNegativeInt = Field(description='Порядковый номер примера с 0 внутри редакции.')
    text: NonEmptyText = Field(description='Сгенерированный пример вопроса по редакции; не источник ответа.')
    dense: list[float] = Field(description='Готовый конечный вектор общего embedding-профиля.')


async def find_chunks(query: str, query_vector: Sequence[float], revision_ids: frozenset[UUID], candidate_limit: int, *, lexical_priority: bool=False) -> list[QdrantHit]:
    """Найти кандидатов Qdrant только в разрешённых редакциях.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        query_vector: Вектор для плотной ветки поиска.
        revision_ids: Разрешённые редакции для обеих веток.
        candidate_limit: Положительный предел числа кандидатов до отбора.
        lexical_priority: Выбранный Searcher профиль: больший вес BM25 для вопроса со ссылкой.

    Returns:
        Список объектов Pydantic `QdrantHit` в порядке поискового профиля. `[]` при пустом наборе редакций или отсутствии совпадений.
    """
    raise NotImplementedError


async def write_revision_points(revision_id: UUID, points: Sequence[ChunkPoint]) -> None:
    """Записать детерминированные точки одной редакции и дождаться подтверждения.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        points: Полный непустой набор точек одной редакции. Размер равен сохранённому expected_chunk_count.

    Returns:
        Одно значение Python, `None`.
    """
    raise NotImplementedError


async def verify_revision_points(revision_id: UUID, expected_chunk_count: int) -> bool:
    """Проверить полный ожидаемый набор детерминированных ID чанков редакции.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: Редакция для точного фильтра payload.
        expected_chunk_count: Число чанков из манифеста. При проверке удаления передаём 0.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError


async def delete_revision_points(revision_id: UUID) -> None:
    """Удалить точки редакции после разрешения владельца состояния.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.

    Returns:
        Одно значение Python, `None`.
    """
    raise NotImplementedError


async def find_topic_matches(query_vector: Sequence[float], revision_ids: frozenset[UUID], limit: int=1) -> list[TopicHit]:
    """Найти ближайшие примеры темы только в закреплённых редакциях.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query_vector: Вектор нормализованного вопроса.
        revision_ids: Тот же набор, который Core передаст Searcher.
        limit: Максимум ближайших примеров.

    Returns:
        Список TopicHit.
    """
    raise NotImplementedError


async def write_topic_points(revision_id: UUID, points: Sequence[TopicPoint]) -> None:
    """Записать полный набор примеров одной редакции и проверить подтверждения.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: Подготовленная редакция.
        points: Полный непустой набор с детерминированными ID.

    Returns:
        None.
    """
    raise NotImplementedError


async def verify_topic_points(revision_id: UUID, expected_topic_count: int) -> bool:
    """Проверить точный набор детерминированных ID примеров, а не только их число.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: Проверяемая редакция.
        expected_topic_count: Ожидание из PostgreSQL, 0 для очистки.

    Returns:
        bool.
    """
    raise NotImplementedError
