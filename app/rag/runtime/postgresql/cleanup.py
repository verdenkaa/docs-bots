"""Contracts: docs/documents/infrastructure/postgresql/cleanup.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from uuid import UUID

from pydantic import Field

from rag.dto import DTO, NonEmptyText

from rag.runtime._validation import _positive, _staging_revision
from ._db import _transaction, _one, _all, _execute, _lock, _corpus


class StagingCleanup(DTO):
    """Завершённое задание с временным Markdown и снимком, ожидающими уборки."""
    revision_id: UUID = Field(description='ID конкретной редакции документа; совпадает с job_id индексации.')
    staging_ref: NonEmptyText = Field(description='Внутренний ключ загрузки в рабочем томе; не пользовательский путь.')


async def list_finished_staging(limit: int) -> list[StagingCleanup]:
    """Найти завершённые задания с ещё не убранным Markdown и снимком подготовки.

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список объектов Pydantic `StagingCleanup`. `[]` означает, что кандидатов нет.
    """
    _positive(limit)
    async with _transaction() as connection:
        rows = await _all(connection,"""SELECT revision_id,staging_ref FROM document_revisions
            WHERE staging_ref IS NOT NULL AND NOT qdrant_write_pending
            AND state IN ('published','failed','retired','deleting','cleaned') ORDER BY revision_id LIMIT :limit""",limit=limit)
        return [StagingCleanup(**row) for row in rows]


async def clear_staging_ref(revision_id: UUID, staging_ref: str) -> bool:
    """Снять ссылку после подтверждённого удаления каталога подготовки.

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Одно логическое значение.
    """
    async with _transaction() as connection:
        row = await _one(connection,"""UPDATE document_revisions SET staging_ref=NULL WHERE revision_id=:id
            AND staging_ref=:ref AND NOT qdrant_write_pending
            AND state IN ('published','failed','retired','deleting','cleaned') RETURNING revision_id""",
            id=revision_id,ref=staging_ref)
        return row is not None


async def is_staging_referenced(staging_ref: str) -> bool:
    """Сверить оставшуюся загрузку с регистрацией в БД под согласованной блокировкой.

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Одно логическое значение.
    """
    revision = _staging_revision(staging_ref)
    async with _transaction() as connection:
        await _lock(connection,"revision",revision)
        return await _one(connection,"SELECT revision_id FROM document_revisions WHERE staging_ref=:ref",ref=staging_ref) is not None


async def claim_revision_cleanup(limit: int) -> list[UUID]:
    """Разрешить очистку ненужных редакций и перевести их в deleting.

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список UUID, включая ранее взятые и незавершённые удаления.
    """
    _positive(limit)
    async with _transaction() as connection:
        await _corpus(connection)
        rows = await _all(connection,"""SELECT d.revision_id FROM document_revisions d
            WHERE NOT d.qdrant_write_pending AND (d.state IN ('failed','deleting') OR
            (d.state='retired' AND NOT EXISTS(SELECT 1 FROM rag_requests r
            WHERE r.pinned_generation>=d.visible_from
            AND (d.visible_until IS NULL OR r.pinned_generation<d.visible_until))))
            ORDER BY d.revision_id LIMIT :limit FOR UPDATE OF d SKIP LOCKED""",limit=limit)
        for row in rows:
            await _execute(connection,"UPDATE document_revisions SET state='deleting' WHERE revision_id=:id",id=row["revision_id"])
        return [row["revision_id"] for row in rows]


async def finish_revision_cleanup(revision_id: UUID) -> bool:
    """Зафиксировать cleaned после подтверждённого удаления точек.

    Args:
        revision_id: ID редакции, равный ID задания индексации.

    Returns:
        Одно логическое значение.
    """
    async with _transaction() as connection:
        row = await _one(connection,"""UPDATE document_revisions SET state='cleaned'
            WHERE revision_id=:id AND state='deleting' AND NOT qdrant_write_pending RETURNING revision_id""",id=revision_id)
        return row is not None
