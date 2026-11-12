"""Contracts: docs/documents/infrastructure/postgresql/ingestion.md."""

from pydantic import Field

from rag.dto import DTO, PositiveId


from datetime import datetime
from uuid import UUID

from rag.ingestion.contracts import AcceptedUpload, ClaimedIngestionJob, IngestionAttempt


class PendingRevisionWrite(DTO):
    """Истёкшая попытка с неизвестным исходом записи Qdrant для операторской сверки."""

    revision_id: UUID = Field(description="ID конкретной редакции документа; совпадает с job_id индексации.")
    attempt_no: PositiveId = Field(
        description="Номер попытки текущего этапа; защищает от записи результата старой попыткой.",
    )


class RevisionManifest(DTO):
    """Ожидаемая полнота редакции при восстановлении индекса из снимка."""

    revision_id: UUID = Field(description="ID конкретной редакции документа; совпадает с job_id индексации.")
    expected_chunk_count: PositiveId | None = Field(
        description="Ожидаемое число чанков; None — полнота старой редакции неизвестна.",
    )


async def register_revision(
    revision_id: UUID,
    document_id: UUID | None,
    staging_ref: str,
) -> AcceptedUpload | None:
    """Зарегистрировать редакцию и outbox после сохранения временной загрузки.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        document_id: ID обновляемого документа. None означает, что нужно выделить новый ID.
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        AcceptedUpload либо None для неизвестного document_id.
        Повтор с тем же revision_id возвращает ранее зарегистрированное задание.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def claim_ingestion_job(job_id: UUID) -> ClaimedIngestionJob | None:
    """Атомарно взять доступное задание и выдать номер и срок попытки.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        job_id: ID из JobRef, равный ID редакции.

    Returns:
        ClaimedIngestionJob либо None: нет работы, занята, завершена или ещё не готова.
        Не берёт редакции с qdrant_write_pending; прежний процесс должен быть остановлен.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def begin_revision_write(attempt: IngestionAttempt, expected_chunk_count: int) -> bool:
    """Сохранить манифест и qdrant_write_pending перед первой записью точек.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        attempt: Право текущей попытки начать запись.
        expected_chunk_count: Положительное число точек полной редакции, из которого восстанавливается набор ID.

    Returns:
        True — запись разрешена; False — попытка не имеет права или признак уже стоит.
        При неизвестном коммите нельзя начинать запись Qdrant.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
        StepError: Код chunk_count_mismatch, retryable=False, если число
            подготовленных чанков отличается от сохранённого.
    """
    raise NotImplementedError


async def publish_revision(attempt: IngestionAttempt) -> int | None:
    """Атомарно опубликовать проверенную редакцию и увеличить поколение корпуса.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        attempt: Активная попытка из claim_ingestion_job.

    Returns:
        Новое поколение либо None, если попытка утратила право публикации.
        Worker заранее подтверждает полноту точек; транзакция снимает pending-признак.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def schedule_ingestion_retry(attempt: IngestionAttempt, retry_at: datetime) -> bool:
    """Вернуть задание в queued и создать отложенный outbox ingestion.jobs.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        attempt: Активная попытка из claim_ingestion_job.
        retry_at: Время, с которого можно повторить работу. Дата и время в UTC.

    Returns:
        True — повтор сохранён; False — переход недопустим.
        Требует qdrant_write_pending=false и действующую попытку.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def fail_ingestion_job(attempt: IngestionAttempt) -> bool:
    """Зафиксировать окончательный сбой индексации действующей попыткой.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        attempt: Активная попытка из claim_ingestion_job.

    Returns:
        True — failed сохранён; False — переход недопустим.
        При qdrant_write_pending=true сначала нужна отдельная сверка.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def list_pending_revision_writes(limit: int) -> list[PendingRevisionWrite]:
    """Выбрать истёкшие записи Qdrant с неизвестным исходом для операторской сверки.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел пачки.

    Returns:
        Список PendingRevisionWrite; вызывается после остановки всех писателей индекса.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def clear_revision_write_pending(revision_id: UUID, attempt_no: int) -> bool:
    """Снять pending-признак после подтверждённой операторской очистки.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        revision_id: Редакция из выборки на сверку.
        attempt_no: Номер той же истёкшей попытки.

    Returns:
        True — снят; False — состояние, номер или срок попытки уже не подходят.
        Не меняет число чанков и попыток; сам по себе не останавливает Qdrant.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def list_required_revision_manifests() -> list[RevisionManifest]:
    """Прочитать манифесты текущего и закреплённых поколений при восстановлении.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Returns:
        Список уникальных RevisionManifest; None в числе чанков означает неизвестную полноту.
        Обычная обработка и изменение индекса на время сверки приостановлены.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError
