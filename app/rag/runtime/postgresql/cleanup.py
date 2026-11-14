"""Contracts: docs/documents/infrastructure/postgresql/cleanup.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from rag.dto import DTO, NonEmptyText

from rag.runtime._validation import _positive, _staging_revision
from ._db import _transaction, _one, _all, _execute, _lock, _corpus

from sqlalchemy import and_, or_, select, update
from ._models import _Request, _Revision


class StagingCleanup(DTO):
    """Завершённое задание с временным Markdown и снимком, ожидающими уборки."""

    revision_id: UUID = Field(
        description="ID конкретной редакции документа; совпадает с job_id индексации."
    )
    staging_ref: NonEmptyText = Field(
        description="Внутренний ключ загрузки в рабочем томе; не пользовательский путь."
    )


async def list_finished_staging(limit: int) -> list[StagingCleanup]:
    """Найти завершённые задания с ещё не убранным Markdown и снимком подготовки.

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список объектов Pydantic `StagingCleanup`. `[]` означает, что кандидатов нет.
    """
    _positive(limit)
    async with _transaction() as session:
        rows = await _all(
            session,
            select(_Revision.revision_id, _Revision.staging_ref)
            .where(
                _Revision.staging_ref.is_not(None),
                _Revision.qdrant_write_pending.is_(False),
                _Revision.state.in_(("published", "failed", "retired", "deleting", "cleaned")),
            )
            .order_by(_Revision.revision_id)
            .limit(limit),
        )
        return [StagingCleanup(**row) for row in rows]


async def clear_staging_ref(revision_id: UUID, staging_ref: str) -> bool:
    """Снять ссылку после подтверждённого удаления каталога подготовки.

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Одно логическое значение.
    """
    async with _transaction() as session:
        row = await _one(
            session,
            update(_Revision)
            .where(
                _Revision.revision_id == revision_id,
                _Revision.staging_ref == staging_ref,
                _Revision.qdrant_write_pending.is_(False),
                _Revision.state.in_(("published", "failed", "retired", "deleting", "cleaned")),
            )
            .values(staging_ref=None)
            .returning(_Revision.revision_id),
        )
        return row is not None


async def is_staging_referenced(staging_ref: str) -> bool:
    """Сверить оставшуюся загрузку с регистрацией в БД под согласованной блокировкой.

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Одно логическое значение.
    """
    revision = _staging_revision(staging_ref)
    async with _transaction() as session:
        await _lock(session, "revision", revision)
        return (
            await _one(
                session, select(_Revision.revision_id).where(_Revision.staging_ref == staging_ref)
            )
            is not None
        )


async def claim_revision_cleanup(limit: int) -> list[UUID]:
    """Разрешить очистку ненужных редакций и перевести их в deleting.

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список UUID, включая ранее взятые и незавершённые удаления.
    """
    _positive(limit)
    async with _transaction() as session:
        await _corpus(session)
        pinned = (
            select(_Request.request_id)
            .where(
                _Request.pinned_generation >= _Revision.visible_from,
                or_(
                    _Revision.visible_until.is_(None),
                    _Request.pinned_generation < _Revision.visible_until,
                ),
            )
            .exists()
        )
        rows = await _all(
            session,
            select(_Revision.revision_id)
            .where(
                _Revision.qdrant_write_pending.is_(False),
                or_(
                    _Revision.state.in_(("failed", "deleting")),
                    and_(_Revision.state == "retired", ~pinned),
                ),
            )
            .order_by(_Revision.revision_id)
            .limit(limit)
            .with_for_update(of=_Revision, skip_locked=True),
        )
        for row in rows:
            await _execute(
                session,
                update(_Revision)
                .where(_Revision.revision_id == row["revision_id"])
                .values(state="deleting"),
            )
        return [row["revision_id"] for row in rows]


async def finish_revision_cleanup(revision_id: UUID) -> bool:
    """Зафиксировать cleaned после подтверждённого удаления точек.

    Args:
        revision_id: ID редакции, равный ID задания индексации.

    Returns:
        Одно логическое значение.
    """
    async with _transaction() as session:
        row = await _one(
            session,
            update(_Revision)
            .where(
                _Revision.revision_id == revision_id,
                _Revision.state == "deleting",
                _Revision.qdrant_write_pending.is_(False),
            )
            .values(state="cleaned")
            .returning(_Revision.revision_id),
        )
        return row is not None
