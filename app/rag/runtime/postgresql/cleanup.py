"""Contracts: docs/documents/infrastructure/postgresql/cleanup.md."""

from pydantic import Field

from rag.dto import DTO, NonEmptyText


from uuid import UUID


class StagingCleanup(DTO):
    """Завершённое задание, временный исходник которого ещё нужно удалить."""

    revision_id: UUID = Field(description="ID конкретной редакции документа; совпадает с job_id индексации.")
    staging_ref: NonEmptyText = Field(description="Внутренний ключ загрузки в рабочем томе; не пользовательский путь.")


async def list_finished_staging(limit: int) -> list[StagingCleanup]:
    """Найти завершённые задания с ещё не убранным временным исходником.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список StagingCleanup, включая опубликованные и неудачные задания.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def clear_staging_ref(revision_id: UUID, staging_ref: str) -> bool:
    """Снять ссылку на временный исходник после подтверждённого удаления.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        True — ссылка очищена; False — ссылка или состояние уже изменились.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def is_staging_referenced(staging_ref: str) -> bool:
    """Сверить оставшуюся загрузку с регистрацией в БД под согласованной блокировкой.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        True — ссылка есть; False — загрузка не привязана к записи.
        Проверку выполняют до возобновления приёма новых загрузок.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def claim_revision_cleanup(limit: int) -> list[UUID]:
    """Разрешить очистку ненужных редакций и перевести их в deleting.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список UUID, включая незавершённые удаления.
        Закреплённые запросами редакции и pending-записи Qdrant исключаются.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def finish_revision_cleanup(revision_id: UUID) -> bool:
    """Зафиксировать cleaned после подтверждённого удаления точек.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.

    Returns:
        True — переход выполнен; False — редакция не находится в deleting.
        Метаданные редакции и её интервалы видимости сохраняются.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError
