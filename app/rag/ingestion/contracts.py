"""Contracts: docs/documents/ingestion/contracts.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from collections.abc import AsyncIterable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from rag.contracts import ImageContent, ImageRef, SourceLocator
from rag.dto import DTO, NonEmptyText, PositiveId, _check_image_markers


class UploadSource(DTO):
    """Название документа для источника; оригинал и исходный URL не хранятся."""
    filename: NonEmptyText = Field(description='Название исходного документа для подписи и выбора обработчика; не доверенный путь.')


@dataclass(frozen=True)
class UploadInput:
    """Технический Python-вход: поток оригинала используется только при конвертации.

    Attributes:
        content: Асинхронные порции исходного документа; в очередь и рабочий том не записываются.
        source: Название источника для конвертации и подписи.
        document_id: ID обновляемого документа; None создаёт новый документ.
    """
    content: AsyncIterable[bytes]
    source: UploadSource
    document_id: UUID | None


class ConvertedImage(DTO):
    """Описание и содержимое картинки, которую Gateway запишет в S3."""
    ref: ImageRef = Field(description='UUID и описание извлечённого изображения.')
    content: ImageContent = Field(description='Проверенные байты и MIME извлечённого изображения; только в памяти вызова.')


class ConversionResult(DTO):
    """Markdown и извлечённые изображения в памяти до сохранения S3."""
    markdown: NonEmptyText = Field(description='Готовый Markdown с неделимыми плейсхолдерами image://UUID.')
    images: list[ConvertedImage] = Field(default_factory=list, description='Извлечённые картинки с байтами в памяти до записи Gateway в S3.')

    @model_validator(mode='after')
    def check_images(self) -> Self:
        """Проверить соответствие Markdown и возвращаемых изображений."""
        _check_image_markers(self.markdown, [image.ref.image_id for image in self.images])
        return self


class ConvertedDocument(DTO):
    """Готовый Markdown и ссылки на подтверждённые изображения, без байтов."""
    markdown: NonEmptyText = Field(description='Готовый Markdown с неделимыми плейсхолдерами image://UUID.')
    images: list[ImageRef] = Field(default_factory=list, description='ID и описания разрешённых изображений; байты здесь не хранятся.')

    @model_validator(mode='after')
    def check_images(self) -> Self:
        """Проверить соответствие Markdown и возвращаемых изображений."""
        _check_image_markers(self.markdown, [image.image_id for image in self.images])
        return self


class JobRef(DTO):
    """Ссылка из очереди на задание индексации; job_id равен revision_id."""
    job_id: UUID = Field(description='ID задания индексации; равен ID создаваемой редакции документа.')


class AcceptedUpload(JobRef):
    """Загрузка сохранена во временном томе, задание и outbox зарегистрированы."""
    kind: Literal['accepted'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    document_id: UUID = Field(description='Стабильный ID документа, общий для всех его редакций.')


class UploadRejected(DTO):
    """Ожидаемый отказ при приёме загрузки с причиной для вызывающего адаптера."""
    kind: Literal['rejected'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    code: Literal['unknown_document', 'invalid_upload', 'too_large', 'unsupported_format', 'conversion_failed'] = Field(description='Стабильный код исхода для вызывающего модуля.')


UploadResult = Annotated[AcceptedUpload | UploadRejected, Field(discriminator='kind')]


class StagedUpload(DTO):
    """Готовый converted.md и манифест временного тома; оригинала нет."""
    path: Path = Field(description='Путь к converted.md внутри рабочего тома; оригинала документа здесь нет.')
    source: UploadSource = Field(description='Название оригинального источника из манифеста.')
    images: list[ImageRef] = Field(default_factory=list, description='ID и описания разрешённых изображений; байты здесь не хранятся.')


class IngestionAttempt(JobRef):
    """Право Worker выполнять текущую попытку индексации до её срока."""
    attempt_no: PositiveId = Field(description='Номер попытки текущего этапа; защищает от записи результата старой попыткой.')
    deadline_at: AwareDatetime = Field(description='Срок действия попытки, datetime с часовым поясом; передаётся в UTC.')


class ClaimedIngestionJob(DTO):
    """Попытка индексации готового Markdown и сохранённые ожидания двух наборов."""
    attempt: IngestionAttempt = Field(description='Активная попытка, полученная при атомарном взятии работы в PostgreSQL.')
    document_id: UUID = Field(description='Стабильный ID документа, общий для всех его редакций.')
    staging_ref: NonEmptyText = Field(description='Внутренний ключ загрузки в рабочем томе; не пользовательский путь.')
    expected_chunk_count: PositiveId | None = Field(default=None, description='Ожидаемое число чанков; None до начала записи индекса.')
    expected_topic_count: PositiveId | None = Field(default=None, description='Ожидаемое число примеров темы; None до начала записи индекса.')

    @model_validator(mode='after')
    def check_expected_counts(self) -> Self:
        """Ожидания двух индексов фиксируются вместе либо ещё отсутствуют."""
        if (self.expected_chunk_count is None) != (self.expected_topic_count is None):
            raise ValueError('Ожидаемые числа чанков и примеров задаются вместе')
        return self


class PreparedChunk(DTO):
    """Текст чанка и его положение после парсинга, до эмбеддинга и записи."""
    text: NonEmptyText = Field(description='Текст фрагмента для последующей обработки.')
    locator: SourceLocator | None = Field(description='Необязательный внутренний локатор; ссылка на раздел не требуется.')
    images: list[ImageRef] = Field(default_factory=list, description='ID и описания разрешённых изображений; байты здесь не хранятся.')

    @model_validator(mode='after')
    def check_images(self) -> Self:
        """Не допустить потерю или подмену ID изображения при разбиении Markdown."""
        _check_image_markers(self.text, [image.image_id for image in self.images])
        return self
