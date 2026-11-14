"""Contracts: docs/documents/infrastructure/postgresql/ingestion.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime, timedelta
from typing import Self
from uuid import UUID, uuid4

from pydantic import Field, model_validator
from sqlalchemy import func, insert, or_, select, union, update

from rag.contracts import StepError
from rag.dto import DTO, NonNegativeInt, PositiveId
from rag.ingestion.contracts import AcceptedUpload, ClaimedIngestionJob, IngestionAttempt, UploadSource
from rag.runtime._state import _get
from rag.runtime import _watchdog
from rag.runtime._validation import _positive, _utc, _text, _staging_revision
from ._db import (
    _transaction,
    _one,
    _all,
    _execute,
    _lock,
    _now,
    _corpus,
    _json,
    _data,
    _outbox,
    _valid_attempt,
    _wake_ingestion,
)

from ._models import _CorpusState, _Request, _Revision


class DocumentVersion(DTO):
    """Опубликованная версия документа, найденная по точному уникальному имени."""
    document_id: UUID = Field(description='Стабильный ID документа; проверяется против document_id задания.')
    document_version: NonNegativeInt = Field(description='Номер опубликованной версии с 0; не поколение корпуса.')


class ReservedRevision(DTO):
    """Назначенная заданию версия; повтор не увеличивает её номер."""
    revision_id: UUID = Field(description='UUID редакции, совпадающий с job_id.')
    document_id: UUID = Field(description='Стабильный ID документа из регистрации задания.')
    document_version: NonNegativeInt = Field(description='0 для первой публикации, иначе base_version + 1.')
    base_version: NonNegativeInt | None = Field(description='Предыдущая опубликованная версия; None, если публикаций ещё нет.')

    @model_validator(mode='after')
    def check_version(self) -> Self:
        """Связать новый номер с опубликованной базой, включая первую версию 0."""
        if self.document_version != (0 if self.base_version is None else self.base_version + 1):
            raise ValueError('Первая версия равна 0, следующая равна base_version + 1')
        return self


class PendingRevisionWrite(DTO):
    """Истёкшая попытка с неизвестным исходом записи Qdrant для операторской сверки."""

    revision_id: UUID = Field(
        description="ID конкретной редакции документа; совпадает с job_id индексации."
    )
    attempt_no: PositiveId = Field(
        description="Номер попытки текущего этапа; защищает от записи результата старой попыткой."
    )


class RevisionManifest(DTO):
    """Ожидаемая полнота редакции при восстановлении индекса из снимка."""

    revision_id: UUID = Field(
        description="ID конкретной редакции документа; совпадает с job_id индексации."
    )
    expected_chunk_count: PositiveId | None = Field(
        description="Ожидаемое число чанков; None до начала записи индекса."
    )
    expected_topic_count: PositiveId | None = Field(
        description="Ожидаемое число примеров темы; None до начала записи индекса."
    )


async def register_revision(
    revision_id: UUID,
    document_id: UUID | None,
    staging_ref: str,
    source: UploadSource,
    image_ids: Sequence[UUID],
) -> AcceptedUpload | None:
    """Зарегистрировать редакцию и outbox после сохранения временной загрузки.

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        document_id: ID нового или обновляемого документа.
        staging_ref: Ключ готового Markdown и манифеста, не оригинала.
        source: Точное имя и уровень доступа готовой партии. При document_id=None имя определяет новый документ или замену.
        image_ids: ID уже сохранённых Gateway изображений; [] допустимо.

    Returns:
        Объект Pydantic `AcceptedUpload` или `None`.
    """
    source = UploadSource.model_validate(
        source.model_dump() if isinstance(source, UploadSource) else source
    )
    source_name = source.filename
    _text(source_name)
    if _staging_revision(staging_ref) != revision_id:
        raise ValueError("Staging reference belongs to a different revision")
    image_ids = list(image_ids)
    if any(not isinstance(image_id, UUID) for image_id in image_ids) or len(set(image_ids)) != len(
        image_ids
    ):
        raise ValueError("Invalid image identifiers")
    async with _transaction() as session:
        await _lock(session, "revision", revision_id)
        old = await _one(session, select(_Revision).where(_Revision.revision_id == revision_id))
        if old is not None:
            if (
                (document_id is not None and document_id != old["document_id"])
                or old["staging_ref"] not in (None, staging_ref)
                or old["source_name"] != source_name
                or _data(old["image_ids"]) != [str(item) for item in image_ids]
            ):
                raise StepError("revision_conflict", False)
            return AcceptedUpload(
                kind="accepted", job_id=revision_id, document_id=old["document_id"]
            )
        selected_document = document_id or uuid4()
        await _lock(session, "document", selected_document)
        previous = await _one(
            session,
            select(func.max(_Revision.revision_no).label("number")).where(
                _Revision.document_id == selected_document
            ),
        )
        if document_id is not None and previous["number"] is None:
            return None
        await _execute(
            session,
            insert(_Revision).values(
                revision_id=revision_id,
                document_id=selected_document,
                revision_no=(previous["number"] or 0) + 1,
                staging_ref=staging_ref,
                source_name=source_name,
                image_ids=_data(_json([str(item) for item in image_ids])),
            ),
        )
        await _outbox(session, "ingestion.jobs", revision_id)
        return AcceptedUpload(kind="accepted", job_id=revision_id, document_id=selected_document)


async def read_document_version(source_name: str) -> DocumentVersion | None:
    """Прочитать опубликованную версию по точному имени документа.

    Заглушка: реализация адаптера остаётся в Runtime.
    None означает отсутствие опубликованной версии, включая ещё не опубликованный документ.
    Сбой чтения поднимает StorageError и не означает первую версию.
    """
    raise NotImplementedError


async def reserve_document_version(attempt: IngestionAttempt, expected_version: int | None) -> ReservedRevision | None:
    """Идемпотентно закрепить новую версию за заданием до подготовки векторов.

    Заглушка: реализация адаптера остаётся в Runtime.
    Под блокировкой документа сверяет опубликованную базу с expected_version.
    При конфликте базы — StepError(document_version_conflict, True), при утрате права — None.
    Повтор той же редакции возвращает прежнюю версию; неизвестный коммит — StorageError.
    """
    raise NotImplementedError


async def claim_ingestion_job(job_id: UUID) -> ClaimedIngestionJob | None:
    """Атомарно взять доступное задание и выдать номер и срок попытки.

    Args:
        job_id: ID из JobRef, равный ID редакции.

    Returns:
        Объект Pydantic `ClaimedIngestionJob` или `None`.
    """
    async with _transaction() as session:
        await _lock(session, "ingestion-worker", 0)
        row = await _one(
            session, select(_Revision).where(_Revision.revision_id == job_id).with_for_update()
        )
        now = await _now(session)
        if row is None or row["state"] not in ("queued", "indexing") or row["qdrant_write_pending"]:
            return None
        if row["resume_at"] is not None and row["resume_at"] > now:
            return None
        previous = await _one(
            session,
            select(_Revision.revision_id)
            .where(
                _Revision.document_id == row["document_id"],
                _Revision.revision_no < row["revision_no"],
                _Revision.state.in_(("queued", "indexing")),
            )
            .limit(1),
        )
        active = await _one(
            session,
            select(_Revision.revision_id)
            .where(
                _Revision.state == "indexing",
                _Revision.resume_at > now,
                _Revision.revision_id != job_id,
            )
            .limit(1),
        )
        if previous is not None or active is not None:
            return None
        deadline = now + timedelta(seconds=_get().settings.ingestion_timeout_s)
        number = row["attempt_no"] + 1
        await _execute(
            session,
            update(_Revision)
            .where(_Revision.revision_id == job_id)
            .values(state="indexing", attempt_no=number, resume_at=deadline),
        )
        attempt = IngestionAttempt(job_id=job_id, attempt_no=number, deadline_at=deadline)
        _watchdog._claimed(attempt)
        return ClaimedIngestionJob(
            attempt=attempt,
            document_id=row["document_id"],
            staging_ref=row["staging_ref"],
            expected_chunk_count=row["expected_chunk_count"],
            expected_topic_count=row["expected_topic_count"],
        )


async def begin_revision_write(
    attempt: IngestionAttempt, expected_chunk_count: int, expected_topic_count: int
) -> bool:
    """Сохранить манифест и qdrant_write_pending перед первой записью точек.

    Args:
        attempt: Право текущей попытки начать запись.
        expected_chunk_count: Положительное число чанков полной редакции.
        expected_topic_count: Положительное число примеров вопросов этой редакции.

    Returns:
        Одно логическое значение.
    """
    _positive(expected_chunk_count)
    _positive(expected_topic_count)
    async with _transaction() as session:
        row = await _one(
            session,
            select(_Revision).where(_Revision.revision_id == attempt.job_id).with_for_update(),
        )
        if (
            not _valid_attempt(row, attempt, "indexing", await _now(session))
            or row["qdrant_write_pending"]
        ):
            return False
        for field, expected, code in (
            ("expected_chunk_count", expected_chunk_count, "chunk_count_mismatch"),
            ("expected_topic_count", expected_topic_count, "topic_count_mismatch"),
        ):
            if row[field] is not None and row[field] != expected:
                raise StepError(code, False)
        await _execute(
            session,
            update(_Revision)
            .where(_Revision.revision_id == attempt.job_id)
            .values(
                expected_chunk_count=expected_chunk_count,
                expected_topic_count=expected_topic_count,
                qdrant_write_pending=True,
            ),
        )
        return True


async def publish_revision(attempt: IngestionAttempt) -> int | None:
    """Атомарно опубликовать проверенную редакцию и увеличить поколение корпуса.

    Args:
        attempt: Активная попытка из claim_ingestion_job.

    Returns:
        Одно целое число или `None`.
    """
    async with _transaction() as session:
        item = await _one(
            session, select(_Revision.document_id).where(_Revision.revision_id == attempt.job_id)
        )
        if item is None:
            return None
        await _lock(session, "document", item["document_id"])
        corpus = await _corpus(session)
        row = await _one(
            session,
            select(_Revision).where(_Revision.revision_id == attempt.job_id).with_for_update(),
        )
        if (
            not _valid_attempt(row, attempt, "indexing", await _now(session))
            or not row["qdrant_write_pending"]
            or not row["expected_chunk_count"]
            or not row["expected_topic_count"]
        ):
            return None
        generation = corpus["generation"] + 1
        await _execute(
            session,
            update(_Revision)
            .where(_Revision.document_id == row["document_id"], _Revision.state == "published")
            .values(state="retired", visible_until=generation),
        )
        await _execute(
            session,
            update(_Revision)
            .where(_Revision.revision_id == attempt.job_id)
            .values(
                state="published",
                visible_from=generation,
                qdrant_write_pending=False,
                resume_at=None,
            ),
        )
        await _execute(
            session,
            update(_CorpusState)
            .where(_CorpusState.singleton_id == 1)
            .values(generation=generation),
        )
        await _wake_ingestion(session, row["document_id"])
        return generation


async def schedule_ingestion_retry(attempt: IngestionAttempt, retry_at: datetime) -> bool:
    """Вернуть задание в queued и создать отложенный outbox ingestion.jobs.

    Args:
        attempt: Активная попытка из claim_ingestion_job.
        retry_at: Время, с которого можно повторить работу. Дата и время в UTC.

    Returns:
        Одно логическое значение.
    """
    _utc(retry_at)
    async with _transaction() as session:
        row = await _one(
            session,
            select(_Revision).where(_Revision.revision_id == attempt.job_id).with_for_update(),
        )
        if (
            not _valid_attempt(row, attempt, "indexing", await _now(session))
            or row["qdrant_write_pending"]
        ):
            return False
        await _execute(
            session,
            update(_Revision)
            .where(_Revision.revision_id == attempt.job_id)
            .values(state="queued", resume_at=retry_at),
        )
        await _outbox(session, "ingestion.jobs", attempt.job_id, retry_at)
        return True


async def fail_ingestion_job(attempt: IngestionAttempt) -> bool:
    """Зафиксировать окончательный сбой индексации действующей попыткой.

    Args:
        attempt: Вход по контракту владельца.

    Returns:
        bool
    """
    async with _transaction() as session:
        row = await _one(
            session,
            select(_Revision).where(_Revision.revision_id == attempt.job_id).with_for_update(),
        )
        if (
            not _valid_attempt(row, attempt, "indexing", await _now(session))
            or row["qdrant_write_pending"]
        ):
            return False
        await _execute(
            session,
            update(_Revision)
            .where(_Revision.revision_id == attempt.job_id)
            .values(state="failed", resume_at=None),
        )
        await _wake_ingestion(session, row["document_id"])
        return True


async def list_pending_revision_writes(limit: int) -> list[PendingRevisionWrite]:
    """Выбрать истёкшие записи Qdrant с неизвестным исходом для операторской сверки.

    Args:
        limit: Положительный предел пачки.

    Returns:
        Список объектов Pydantic `PendingRevisionWrite`. Пустой список допустим.
    """
    _positive(limit)
    async with _transaction() as session:
        rows = await _all(
            session,
            select(_Revision.revision_id, _Revision.attempt_no)
            .where(
                _Revision.state == "indexing",
                _Revision.qdrant_write_pending.is_(True),
                _Revision.resume_at <= func.clock_timestamp(),
            )
            .order_by(_Revision.revision_id)
            .limit(limit)
            .with_for_update(skip_locked=True),
        )
        return [PendingRevisionWrite(**row) for row in rows]


async def clear_revision_write_pending(revision_id: UUID, attempt_no: int) -> bool:
    """Снять pending-признак после подтверждённой операторской очистки.

    Args:
        revision_id: Редакция из выборки на сверку.
        attempt_no: Номер той же истёкшей попытки.

    Returns:
        Одно логическое значение.
    """
    _positive(attempt_no)
    async with _transaction() as session:
        row = await _one(
            session, select(_Revision).where(_Revision.revision_id == revision_id).with_for_update()
        )
        if (
            row is None
            or row["state"] != "indexing"
            or not row["qdrant_write_pending"]
            or row["attempt_no"] != attempt_no
            or row["resume_at"] > await _now(session)
        ):
            return False
        await _execute(
            session,
            update(_Revision)
            .where(_Revision.revision_id == revision_id)
            .values(qdrant_write_pending=False),
        )
        return True


async def list_required_revision_manifests() -> list[RevisionManifest]:
    """Прочитать манифесты текущего и закреплённых поколений при восстановлении.

    Returns:
        Список объектов Pydantic `RevisionManifest`. Пустой список означает, что ни одна редакция не нужна поиску.
    """
    async with _transaction() as session:
        generations = union(
            select(_CorpusState.generation),
            select(_Request.pinned_generation.label("generation")).where(
                _Request.pinned_generation.is_not(None)
            ),
        ).subquery()
        needed = (
            select(generations.c.generation)
            .where(
                _Revision.visible_from <= generations.c.generation,
                or_(
                    _Revision.visible_until.is_(None),
                    generations.c.generation < _Revision.visible_until,
                ),
            )
            .exists()
        )
        rows = await _all(
            session,
            select(
                _Revision.revision_id,
                _Revision.expected_chunk_count,
                _Revision.expected_topic_count,
            )
            .where(needed)
            .distinct(),
        )
        return [RevisionManifest(**row) for row in rows]
