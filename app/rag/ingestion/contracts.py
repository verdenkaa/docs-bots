"""Contracts: docs/documents/ingestion/contracts.md."""

from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonEmptyText


from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

from rag.contracts import SourceLocator


class UploadSource(DTO):
    """Название и необязательная исходная ссылка загружаемого документа."""

    filename: NonEmptyText = Field(
        description="Исходное имя файла для парсинга и метаданных; не доверенный путь на диске.",
    )
    source_url: str | None = Field(description="Исходная ссылка на документ; None, если её не предоставили.")


@dataclass(frozen=True)
class UploadInput:
    """Технический Python-вход загрузки с потоком байтов; не транспортный JSON DTO.

    Gateway проверяет поток и метаданные; dataclass не выполняет Pydantic-валидацию.

    Attributes:
        content: Асинхронный поток порций файла с проверкой размера в Gateway.
        source: Имя файла и исходная ссылка, сохраняемые вместе с загрузкой.
        document_id: ID обновляемого документа; None создаёт новый документ.
    """

    #: Асинхронный поток порций файла; Gateway читает его с ограничением размера.
    content: AsyncIterable[bytes]
    #: Имя файла и исходная ссылка, сохранённые вместе с загрузкой.
    source: UploadSource
    #: ID обновляемого документа; None означает создание нового документа.
    document_id: UUID | None


class JobRef(DTO):
    """Ссылка из очереди на задание индексации; job_id равен revision_id."""

    job_id: UUID = Field(description="ID задания индексации; равен ID создаваемой редакции документа.")


class AcceptedUpload(JobRef):
    """Загрузка сохранена во временном томе, задание и outbox зарегистрированы."""

    kind: Literal["accepted"] = Field(description="Метка исхода; определяет вариант DTO при разборе объединения.")
    document_id: UUID = Field(description="Стабильный ID документа, общий для всех его редакций.")


class UploadRejected(DTO):
    """Ожидаемый отказ при приёме загрузки с причиной для вызывающего адаптера."""

    kind: Literal["rejected"] = Field(description="Метка исхода; определяет вариант DTO при разборе объединения.")
    code: Literal["unknown_document", "invalid_upload", "too_large"] = Field(
        description="Машиночитаемая причина отказа для вызывающего адаптера.",
    )


UploadResult = Annotated[AcceptedUpload | UploadRejected, Field(discriminator="kind")]


class StagedUpload(DTO):
    """Открытая временная загрузка для парсера; это не постоянный архив оригиналов."""

    path: Path = Field(description="Локальный путь к исходнику внутри рабочего тома Worker.")
    source: UploadSource = Field(description="Имя файла и исходная ссылка, сохранённые вместе с загрузкой.")


class IngestionAttempt(JobRef):
    """Право Worker выполнять текущую попытку индексации до её срока."""

    attempt_no: PositiveId = Field(
        description="Номер попытки текущего этапа; защищает от записи результата старой попыткой.",
    )
    deadline_at: AwareDatetime = Field(
        description="Срок действия попытки, datetime с часовым поясом; передаётся в UTC.",
    )


class ClaimedIngestionJob(DTO):
    """Взятое Worker задание со ссылкой на исходник во временном томе."""

    attempt: IngestionAttempt = Field(
        description="Активная попытка, полученная при атомарном взятии работы в PostgreSQL.",
    )
    document_id: UUID = Field(description="Стабильный ID документа, общий для всех его редакций.")
    staging_ref: NonEmptyText = Field(description="Внутренний ключ загрузки в рабочем томе; не пользовательский путь.")


class PreparedChunk(DTO):
    """Текст чанка и его положение после парсинга, до эмбеддинга и записи."""

    text: NonEmptyText = Field(description="Текст фрагмента для последующей обработки.")
    locator: SourceLocator | None = Field(
        description="Положение в документе; None, если парсер не смог его определить.",
    )

