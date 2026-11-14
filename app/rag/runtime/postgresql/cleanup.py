"""Contracts: docs/documents/infrastructure/postgresql/cleanup.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from uuid import UUID

from pydantic import Field

from rag.dto import DTO, NonEmptyText


class StagingCleanup(DTO):
    """Завершённое задание с временным Markdown и снимком, ожидающими уборки."""
    revision_id: UUID = Field(description='ID конкретной редакции документа; совпадает с job_id индексации.')
    staging_ref: NonEmptyText = Field(description='Внутренний ключ загрузки в рабочем томе; не пользовательский путь.')


async def list_finished_staging(limit: int) -> list[StagingCleanup]:
    """Найти завершённые задания с ещё не убранным Markdown и снимком подготовки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список объектов Pydantic `StagingCleanup`. `[]` означает, что кандидатов нет.
    """
    raise NotImplementedError


async def clear_staging_ref(revision_id: UUID, staging_ref: str) -> bool:
    """Снять ссылку после подтверждённого удаления каталога подготовки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError


async def is_staging_referenced(staging_ref: str) -> bool:
    """Сверить оставшуюся загрузку с регистрацией в БД под согласованной блокировкой.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError


async def claim_revision_cleanup(limit: int) -> list[UUID]:
    """Разрешить очистку ненужных редакций и перевести их в deleting.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список UUID, включая ранее взятые и незавершённые удаления.
    """
    raise NotImplementedError


async def finish_revision_cleanup(revision_id: UUID) -> bool:
    """Зафиксировать cleaned после подтверждённого удаления точек.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError
