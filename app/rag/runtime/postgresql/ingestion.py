"""Contracts: docs/documents/infrastructure/postgresql/ingestion.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from uuid import UUID

from pydantic import Field

from rag.dto import DTO, PositiveId
from rag.ingestion.contracts import AcceptedUpload, ClaimedIngestionJob, IngestionAttempt


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

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        document_id: ID нового или обновляемого документа.
        staging_ref: Ключ готового Markdown и манифеста, не оригинала.
        source_name: Название исходного документа из UploadSource.filename для чанков и HTML-заглушки.
        image_ids: ID уже сохранённых Gateway изображений; [] допустимо.

    Returns:
        Объект Pydantic `AcceptedUpload` или `None`.
    """
    raise NotImplementedError


async def claim_ingestion_job(job_id: UUID) -> ClaimedIngestionJob | None:
    """Атомарно взять доступное задание и выдать номер и срок попытки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        job_id: ID из JobRef, равный ID редакции.

    Returns:
        Объект Pydantic `ClaimedIngestionJob` или `None`.
    """
    raise NotImplementedError


async def begin_revision_write(attempt: IngestionAttempt, expected_chunk_count: int, expected_topic_count: int) -> bool:
    """Сохранить манифест и qdrant_write_pending перед первой записью точек.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Право текущей попытки начать запись.
        expected_chunk_count: Положительное число чанков полной редакции.
        expected_topic_count: Положительное число примеров вопросов этой редакции.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError


async def publish_revision(attempt: IngestionAttempt) -> int | None:
    """Атомарно опубликовать проверенную редакцию и увеличить поколение корпуса.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Активная попытка из claim_ingestion_job.

    Returns:
        Одно целое число или `None`.
    """
    raise NotImplementedError


async def schedule_ingestion_retry(attempt: IngestionAttempt, retry_at: datetime) -> bool:
    """Вернуть задание в queued и создать отложенный outbox ingestion.jobs.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Активная попытка из claim_ingestion_job.
        retry_at: Время, с которого можно повторить работу. Дата и время в UTC.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError


async def fail_ingestion_job(attempt: IngestionAttempt) -> bool:
    """Зафиксировать окончательный сбой индексации действующей попыткой.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Вход по контракту владельца.

    Returns:
        bool
    """
    raise NotImplementedError


async def list_pending_revision_writes(limit: int) -> list[PendingRevisionWrite]:
    """Выбрать истёкшие записи Qdrant с неизвестным исходом для операторской сверки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел пачки.

    Returns:
        Список объектов Pydantic `PendingRevisionWrite`. Пустой список допустим.
    """
    raise NotImplementedError


async def clear_revision_write_pending(revision_id: UUID, attempt_no: int) -> bool:
    """Снять pending-признак после подтверждённой операторской очистки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: Редакция из выборки на сверку.
        attempt_no: Номер той же истёкшей попытки.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError


async def list_required_revision_manifests() -> list[RevisionManifest]:
    """Прочитать манифесты текущего и закреплённых поколений при восстановлении.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Returns:
        Список объектов Pydantic `RevisionManifest`. Пустой список означает, что ни одна редакция не нужна поиску.
    """
    raise NotImplementedError
