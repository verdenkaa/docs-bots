"""Contracts: docs/documents/infrastructure/qdrant/contracts.md."""

from pydantic import Field

from rag.dto import DTO, NonNegativeInt


from collections.abc import Sequence
from uuid import UUID

from rag.contracts import SearchFragment


class ChunkPoint(SearchFragment):
    """Точка Qdrant: текст, метаданные, номер чанка и готовый плотный вектор."""

    chunk_no: NonNegativeInt = Field(
        description="Порядковый номер чанка с 0 внутри редакции; участвует в детерминированном ID.",
    )
    dense: list[float] = Field(description="Готовый плотный вектор общего embedding-профиля индекса и поиска.")


class QdrantHit(DTO):
    """Кандидат Qdrant до окончательного отбора и реранкинга Searcher."""

    fragment: SearchFragment = Field(description="Текст кандидата и метаданные его источника.")
    score: float = Field(description="Оценка модели или поиска; шкала определяется конкретным профилем.")


async def find_chunks(
    query: str,
    query_vector: Sequence[float],
    revision_ids: frozenset[UUID],
    candidate_limit: int,
) -> list[QdrantHit]:
    """Найти кандидатов Qdrant только в разрешённых редакциях.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        query_vector: Вектор для плотной ветки поиска.
        revision_ids: Разрешённые редакции для обеих веток.
        candidate_limit: Положительный предел числа кандидатов до отбора.

    Returns:
        Список QdrantHit до окончательного отбора Searcher; пустой список допустим.
    """
    raise NotImplementedError


async def write_revision_points(revision_id: UUID, points: Sequence[ChunkPoint]) -> None:
    """Записать детерминированные точки одной редакции и дождаться подтверждения.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        points: Полный непустой набор точек одной редакции. Размер равен сохранённому expected_chunk_count.

    Returns:
        None после подтверждения. Таймаут не доказывает прекращение записи
        на сервере; повтор и очистка требуют протокола сверки.
    """
    raise NotImplementedError


async def verify_revision_points(revision_id: UUID, expected_chunk_count: int) -> bool:
    """Проверить полный ожидаемый набор детерминированных ID чанков редакции.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        revision_id: Редакция для точного фильтра payload.
        expected_chunk_count: Число чанков из манифеста. При проверке удаления передаём 0.

    Returns:
        True — полный набор подтверждён; False — полнота не подтверждена.
        Одного наличия revision_id или совпадения количества недостаточно.
    """
    raise NotImplementedError


async def delete_revision_points(revision_id: UUID) -> None:
    """Удалить точки редакции после разрешения владельца состояния.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.

    Returns:
        None после подтверждённого удаления; нельзя удалять при незавершённой записи.
    """
    raise NotImplementedError
