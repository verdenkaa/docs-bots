"""Contracts: docs/documents/infrastructure/postgresql/ingestion.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import Field

from rag.dto import DTO, PositiveId
from rag.ingestion.contracts import AcceptedUpload, ClaimedIngestionJob, IngestionAttempt

from datetime import timedelta
from uuid import uuid4
from rag.contracts import StepError
from rag.runtime._state import _get
from rag.runtime import _watchdog
from rag.runtime._validation import _positive, _utc, _text, _staging_revision
from ._db import (_transaction, _one, _all, _execute, _lock, _now, _corpus,
                  _json, _data, _outbox, _valid_attempt, _wake_ingestion)


class PendingRevisionWrite(DTO):
    """Истёкшая попытка с неизвестным исходом записи Qdrant для операторской сверки."""
    revision_id: UUID = Field(description='ID конкретной редакции документа; совпадает с job_id индексации.')
    attempt_no: PositiveId = Field(description='Номер попытки текущего этапа; защищает от записи результата старой попыткой.')


class RevisionManifest(DTO):
    """Ожидаемая полнота редакции при восстановлении индекса из снимка."""
    revision_id: UUID = Field(description='ID конкретной редакции документа; совпадает с job_id индексации.')
    expected_chunk_count: PositiveId | None = Field(description='Ожидаемое число чанков; None до начала записи индекса.')
    expected_topic_count: PositiveId | None = Field(description='Ожидаемое число примеров темы; None до начала записи индекса.')


async def register_revision(revision_id: UUID, document_id: UUID | None, staging_ref: str, source_name: str, image_ids: Sequence[UUID]) -> AcceptedUpload | None:
    """Зарегистрировать редакцию и outbox после сохранения временной загрузки.

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        document_id: ID нового или обновляемого документа.
        staging_ref: Ключ готового Markdown и манифеста, не оригинала.
        source_name: Название исходного документа из UploadSource.filename для чанков и HTML-заглушки.
        image_ids: ID уже сохранённых Gateway изображений; [] допустимо.

    Returns:
        Объект Pydantic `AcceptedUpload` или `None`.
    """
    _text(source_name)
    if _staging_revision(staging_ref) != revision_id:
        raise ValueError("Staging reference belongs to a different revision")
    image_ids = list(image_ids)
    if any(not isinstance(image_id, UUID) for image_id in image_ids) or len(set(image_ids)) != len(image_ids):
        raise ValueError("Invalid image identifiers")
    async with _transaction() as connection:
        await _lock(connection, "revision", revision_id)
        old = await _one(connection,"SELECT * FROM document_revisions WHERE revision_id=:id",id=revision_id)
        if old is not None:
            if ((document_id is not None and document_id != old["document_id"]) or
                old["staging_ref"] not in (None, staging_ref) or old["source_name"] != source_name or
                _data(old["image_ids"]) != [str(item) for item in image_ids]):
                raise StepError("revision_conflict", False)
            return AcceptedUpload(kind="accepted",job_id=revision_id,document_id=old["document_id"])
        selected_document = document_id or uuid4()
        await _lock(connection,"document",selected_document)
        previous = await _one(connection,"SELECT max(revision_no) AS number FROM document_revisions WHERE document_id=:id",id=selected_document)
        if document_id is not None and previous["number"] is None:
            return None
        await _execute(connection,"""INSERT INTO document_revisions
            (revision_id,document_id,revision_no,staging_ref,source_name,image_ids)
            VALUES(:revision,:document,:number,:staging,:name,CAST(:images AS jsonb))""",
            revision=revision_id,document=selected_document,number=(previous["number"] or 0)+1,
            staging=staging_ref,name=source_name,images=_json([str(item) for item in image_ids]))
        await _outbox(connection,"ingestion.jobs",revision_id)
        return AcceptedUpload(kind="accepted",job_id=revision_id,document_id=selected_document)


async def claim_ingestion_job(job_id: UUID) -> ClaimedIngestionJob | None:
    """Атомарно взять доступное задание и выдать номер и срок попытки.

    Args:
        job_id: ID из JobRef, равный ID редакции.

    Returns:
        Объект Pydantic `ClaimedIngestionJob` или `None`.
    """
    async with _transaction() as connection:
        await _lock(connection,"ingestion-worker",0)
        row = await _one(connection,"SELECT * FROM document_revisions WHERE revision_id=:id FOR UPDATE",id=job_id)
        now = await _now(connection)
        if row is None or row["state"] not in ("queued","indexing") or row["qdrant_write_pending"]:
            return None
        if row["resume_at"] is not None and row["resume_at"] > now:
            return None
        previous = await _one(connection,"""SELECT revision_id FROM document_revisions WHERE document_id=:document
            AND revision_no<:number AND state IN ('queued','indexing') LIMIT 1""",
            document=row["document_id"],number=row["revision_no"])
        active = await _one(connection,"""SELECT revision_id FROM document_revisions
            WHERE state='indexing' AND resume_at>:now AND revision_id!=:id LIMIT 1""",now=now,id=job_id)
        if previous is not None or active is not None:
            return None
        deadline = now + timedelta(seconds=_get().settings.ingestion_timeout_s)
        number = row["attempt_no"] + 1
        await _execute(connection,"""UPDATE document_revisions SET state='indexing',attempt_no=:number,
            resume_at=:deadline WHERE revision_id=:id""",number=number,deadline=deadline,id=job_id)
        attempt = IngestionAttempt(job_id=job_id,attempt_no=number,deadline_at=deadline)
        _watchdog._claimed(attempt)
        return ClaimedIngestionJob(attempt=attempt,
            document_id=row["document_id"],staging_ref=row["staging_ref"],
            expected_chunk_count=row["expected_chunk_count"],expected_topic_count=row["expected_topic_count"])


async def begin_revision_write(attempt: IngestionAttempt, expected_chunk_count: int, expected_topic_count: int) -> bool:
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
    async with _transaction() as connection:
        row = await _one(connection,"SELECT * FROM document_revisions WHERE revision_id=:id FOR UPDATE",id=attempt.job_id)
        if not _valid_attempt(row,attempt,"indexing",await _now(connection)) or row["qdrant_write_pending"]:
            return False
        for field, expected, code in (("expected_chunk_count",expected_chunk_count,"chunk_count_mismatch"),
                                       ("expected_topic_count",expected_topic_count,"topic_count_mismatch")):
            if row[field] is not None and row[field] != expected:
                raise StepError(code,False)
        await _execute(connection,"""UPDATE document_revisions SET expected_chunk_count=:chunks,
            expected_topic_count=:topics,qdrant_write_pending=true WHERE revision_id=:id""",
            chunks=expected_chunk_count,topics=expected_topic_count,id=attempt.job_id)
        return True


async def publish_revision(attempt: IngestionAttempt) -> int | None:
    """Атомарно опубликовать проверенную редакцию и увеличить поколение корпуса.

    Args:
        attempt: Активная попытка из claim_ingestion_job.

    Returns:
        Одно целое число или `None`.
    """
    async with _transaction() as connection:
        item = await _one(connection,"SELECT document_id FROM document_revisions WHERE revision_id=:id",id=attempt.job_id)
        if item is None:
            return None
        await _lock(connection,"document",item["document_id"])
        corpus = await _corpus(connection)
        row = await _one(connection,"SELECT * FROM document_revisions WHERE revision_id=:id FOR UPDATE",id=attempt.job_id)
        if (not _valid_attempt(row,attempt,"indexing",await _now(connection)) or
            not row["qdrant_write_pending"] or not row["expected_chunk_count"] or not row["expected_topic_count"]):
            return None
        generation = corpus["generation"] + 1
        await _execute(connection,"""UPDATE document_revisions SET state='retired',visible_until=:generation
            WHERE document_id=:document AND state='published'""",generation=generation,document=row["document_id"])
        await _execute(connection,"""UPDATE document_revisions SET state='published',visible_from=:generation,
            qdrant_write_pending=false,resume_at=NULL WHERE revision_id=:id""",generation=generation,id=attempt.job_id)
        await _execute(connection,"UPDATE corpus_state SET generation=:generation WHERE singleton_id=1",generation=generation)
        await _wake_ingestion(connection,row["document_id"])
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
    async with _transaction() as connection:
        row = await _one(connection,"SELECT * FROM document_revisions WHERE revision_id=:id FOR UPDATE",id=attempt.job_id)
        if not _valid_attempt(row,attempt,"indexing",await _now(connection)) or row["qdrant_write_pending"]:
            return False
        await _execute(connection,"UPDATE document_revisions SET state='queued',resume_at=:at WHERE revision_id=:id",
            at=retry_at,id=attempt.job_id)
        await _outbox(connection,"ingestion.jobs",attempt.job_id,retry_at)
        return True


async def fail_ingestion_job(attempt: IngestionAttempt) -> bool:
    """Зафиксировать окончательный сбой индексации действующей попыткой.

    Args:
        attempt: Вход по контракту владельца.

    Returns:
        bool
    """
    async with _transaction() as connection:
        row = await _one(connection,"SELECT * FROM document_revisions WHERE revision_id=:id FOR UPDATE",id=attempt.job_id)
        if not _valid_attempt(row,attempt,"indexing",await _now(connection)) or row["qdrant_write_pending"]:
            return False
        await _execute(connection,"UPDATE document_revisions SET state='failed',resume_at=NULL WHERE revision_id=:id",id=attempt.job_id)
        await _wake_ingestion(connection,row["document_id"])
        return True


async def list_pending_revision_writes(limit: int) -> list[PendingRevisionWrite]:
    """Выбрать истёкшие записи Qdrant с неизвестным исходом для операторской сверки.

    Args:
        limit: Положительный предел пачки.

    Returns:
        Список объектов Pydantic `PendingRevisionWrite`. Пустой список допустим.
    """
    _positive(limit)
    async with _transaction() as connection:
        rows = await _all(connection,"""SELECT revision_id,attempt_no FROM document_revisions
            WHERE state='indexing' AND qdrant_write_pending AND resume_at<=clock_timestamp()
            ORDER BY revision_id LIMIT :limit FOR UPDATE SKIP LOCKED""",limit=limit)
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
    async with _transaction() as connection:
        row = await _one(connection,"SELECT * FROM document_revisions WHERE revision_id=:id FOR UPDATE",id=revision_id)
        if (row is None or row["state"]!="indexing" or not row["qdrant_write_pending"] or
            row["attempt_no"]!=attempt_no or row["resume_at"]>await _now(connection)):
            return False
        await _execute(connection,"UPDATE document_revisions SET qdrant_write_pending=false WHERE revision_id=:id",id=revision_id)
        return True


async def list_required_revision_manifests() -> list[RevisionManifest]:
    """Прочитать манифесты текущего и закреплённых поколений при восстановлении.

    Returns:
        Список объектов Pydantic `RevisionManifest`. Пустой список означает, что ни одна редакция не нужна поиску.
    """
    async with _transaction() as connection:
        rows = await _all(connection,"""SELECT DISTINCT d.revision_id,d.expected_chunk_count,d.expected_topic_count
            FROM document_revisions d WHERE EXISTS (
            SELECT 1 FROM (SELECT generation FROM corpus_state UNION SELECT pinned_generation AS generation
            FROM rag_requests WHERE pinned_generation IS NOT NULL) AS generations
            WHERE d.visible_from<=generations.generation
            AND (d.visible_until IS NULL OR generations.generation<d.visible_until))""")
        return [RevisionManifest(**row) for row in rows]
