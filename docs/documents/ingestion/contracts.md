---
title: DTO и порядок индексации
sidebar_position: 5
---

[Правила DTO и Pydantic](../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.

[Точки входа Ingestion](./entrypoints.md).

## DTO

`SourceLocator` — [общий тип локатора фрагмента](../rag-core/contracts.md#общие-dto). `ConsumeDecision` — [решение о подтверждении RabbitMQ](../rag-core/contracts.md#очередь-и-готовый-результат). Для технических ошибок используем общие [`StorageError` и `StepError`](../rag-core/contracts.md#исходы-шагов).

~~~python
from typing import Self
from pydantic import model_validator
from rag.dto import _check_image_markers

from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText
from rag.contracts import ImageRef, ImageContent, SourceLocator
from dataclasses import dataclass
from collections.abc import AsyncIterable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID

class UploadSource(DTO):
    filename: NonEmptyText

@dataclass(frozen=True)
class UploadInput:
    content: AsyncIterable[bytes]
    source: UploadSource
    document_id: UUID | None

class ConvertedImage(DTO):
    ref: ImageRef
    content: ImageContent

class ConversionResult(DTO):
    markdown: NonEmptyText
    images: list[ConvertedImage] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_images(self) -> Self:
        """Проверить соответствие Markdown и возвращаемых изображений."""
        _check_image_markers(self.markdown, [image.ref.image_id for image in self.images])
        return self

class ConvertedDocument(DTO):
    markdown: NonEmptyText
    images: list[ImageRef] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_images(self) -> Self:
        """Проверить соответствие Markdown и возвращаемых изображений."""
        _check_image_markers(self.markdown, [image.image_id for image in self.images])
        return self

class JobRef(DTO):
    job_id: UUID

class AcceptedUpload(JobRef):
    kind: Literal['accepted']
    document_id: UUID

class UploadRejected(DTO):
    kind: Literal['rejected']
    code: Literal['unknown_document', 'invalid_upload', 'too_large', 'unsupported_format', 'conversion_failed']
UploadResult = Annotated[AcceptedUpload | UploadRejected, Field(discriminator='kind')]

class StagedUpload(DTO):
    path: Path
    source: UploadSource
    images: list[ImageRef] = Field(default_factory=list)

class IngestionAttempt(JobRef):
    attempt_no: PositiveId
    deadline_at: AwareDatetime

class ClaimedIngestionJob(DTO):
    attempt: IngestionAttempt
    document_id: UUID
    staging_ref: NonEmptyText
    expected_chunk_count: PositiveId | None = None
    expected_topic_count: PositiveId | None = None

    @model_validator(mode='after')
    def check_expected_counts(self) -> Self:
        """Ожидания двух индексов фиксируются вместе либо ещё отсутствуют."""
        if (self.expected_chunk_count is None) != (self.expected_topic_count is None):
            raise ValueError('Ожидаемые числа чанков и примеров задаются вместе')
        return self

class PreparedChunk(DTO):
    text: NonEmptyText
    locator: SourceLocator | None
    images: list[ImageRef] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_images(self) -> Self:
        """Не допустить потерю или подмену ID изображения при разбиении Markdown."""
        _check_image_markers(self.text, [image.image_id for image in self.images])
        return self
~~~


| DTO / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| UploadSource.filename | NonEmptyText | Название оригинала, не путь. | Руководство.docx |
| ConversionResult.markdown | NonEmptyText | Готовый Markdown с плейсхолдерами. | `![Схема](image://…)` |
| ConversionResult.images | list[ConvertedImage] | Изображения в памяти до S3; ref и content. | [] |
| ConvertedImage.ref | ImageRef | UUID и описание, совпадающие с Markdown. | ImageRef(...) |
| ConvertedImage.content | ImageContent | Проверенные байты и MIME. | image/png |
| ConvertedDocument.images | list[ImageRef] | Только подтверждённые S3-объекты; без байтов. | [] |
| StagedUpload.path / source / images | Path / UploadSource / список | converted.md и манифест от Runtime. | Path(".../converted.md") |
| PreparedChunk.text / locator / images | текст / локатор или None / список | Контекст и изображения именно этого чанка. | locator=None, images=[] |

ID изображений уникальны; набор плейсхолдеров равен списку изображений, описания согласованы. В реализации это обязательные валидаторы/проверки границ. Оригинал, потоки и байты не попадают в очередь. `job_id = revision_id`.

Коды отказа: invalid_upload — пустой/неверный ввод, too_large — размер, unsupported_format — нет обработчика, conversion_failed — окончательный сбой разбора, unknown_document — неизвестный ID замены. Временный отказ модели/хранилища остаётся StepError(retryable=True) или StorageError.

## Цепочка загрузки и публикации

| Кто вызывает | Вход и источник | Выход / следующий шаг |
| --- | --- | --- |
| Gateway → Converter | convert_document(upload, revision_id), UUID назначил Gateway. | ConversionResult: Markdown и изображения в памяти. |
| Gateway → S3 | put_image(item.ref.image_id, item.content). | Подтверждение каждой записи; ConvertedDocument без байтов. |
| Gateway → том | stage_upload(revision_id, document, upload.source). | Ключ устойчивого Markdown/манифеста. |
| Gateway → PostgreSQL | register_revision(revision_id, upload.document_id, staging_ref, upload.source.filename, image_ids). | AcceptedUpload, редакция/outbox. |
| Relay → Worker | JobRef из ingestion.jobs. | Claim с попыткой, document_id и staging_ref. |
| Worker → том | open_staged_upload; load_prepared_revision. | Markdown/метаданные и сохранённый набор либо None. |
| Worker → подготовка | При отсутствии набора: prepare_chunks, embedding document, генерация примеров, embedding query. | Полные ChunkPoint и TopicPoint. |
| Worker → том | store_prepared_revision(staging_ref, prepared) до записи Qdrant. | Устойчивый неизменяемый PreparedRevision для повторов. |
| Worker → PostgreSQL | begin_revision_write(attempt, len(points), len(topic_points)). | Оба числа и qdrant_write_pending сохранены. |
| Worker → Qdrant | write_revision_points, write_topic_points, обе verify-функции. | Оба полных набора подтверждены. |
| Worker → PostgreSQL | publish_revision(attempt). | Новое поколение для темы и поиска. |

[Подготовленный набор](../infrastructure/upload-spool/contracts.md#prepared-revision) · [Сквозной поток](../rag-core/flow.md#стык-с-ingestion)

## Сборка точки Qdrant

Worker собирает один [`ChunkPoint`](../infrastructure/qdrant/contracts.md#dto) на каждый `PreparedChunk`.

| Поле `ChunkPoint` | Источник |
| --- | --- |
| `document_id` | `job.document_id` из PostgreSQL. |
| `revision_id` | `job.attempt.job_id` из PostgreSQL, тот же ID, что в сообщении. |
| `chunk_no` | Индекс чанка в результате `prepare_chunks`, начиная с 0. |
| `chunk_id` | `uuid5(revision_id, str(chunk_no))`. При повторе получаем те же ID. |
| `text`, `locator`, `images` | Поля PreparedChunk. images содержит ID/описания плейсхолдеров этого чанка; повтор не назначает новые ID. |
| `source_name`, `source_url` | `upload.source.filename` из манифеста; source_url=None, ссылку на HTML-заглушку строит RAG API. |
| `dense` | Вектор из `EmbeddingResult.vectors` на позиции `chunk_no`. |

Адаптер Qdrant сериализует точку. Текст для BM25 берётся из того же поля `text`. [Пример записи](../infrastructure/qdrant/index.md#структура-точки)

TopicPoint собирается из валидированного списка примеров: question_no с 0, UUIDv5 редакции и строки `question:<номер>`, тот же revision_id, текст и вектор purpose=query. Чанки и примеры используют один embedding-профиль.

ClaimedIngestionJob.expected_chunk_count и expected_topic_count — сохранённые ожидания после первой begin_revision_write, либо оба None до неё. Если ожидания уже есть, но PreparedRevision утрачен, Worker не генерирует замену незаметно: ошибка подготовки/операторская проверка. Это защищает совпадение повторных ID при отличающемся содержимом.
