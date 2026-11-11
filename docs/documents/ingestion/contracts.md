---
title: DTO и порядок индексации
sidebar_position: 5
---

:::warning Рабочая спецификация
Python-интерфейсы заданы для MVP. Внешний способ загрузки, форматы файлов, лимиты и парсеры выбираем по кейсу.
:::

[Правила DTO и Pydantic](../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.

[Точки входа Ingestion](./entrypoints.md).

## DTO

`SourceLocator` — [общий тип локатора фрагмента](../rag-core/contracts.md#общие-dto). `ConsumeDecision` — [решение о подтверждении RabbitMQ](../rag-core/contracts.md#очередь-и-готовый-результат). Для технических ошибок используем общие [`StorageError` и `StepError`](../rag-core/contracts.md#исходы-шагов).

~~~python
from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText
from dataclasses import dataclass

from collections.abc import AsyncIterable
from datetime import datetime
from pathlib import Path
from typing import Annotated, Literal
from uuid import UUID


class UploadSource(DTO):
    filename: NonEmptyText
    source_url: str | None


@dataclass(frozen=True)
class UploadInput:
    content: AsyncIterable[bytes]
    source: UploadSource
    document_id: UUID | None


class JobRef(DTO):
    job_id: UUID


class AcceptedUpload(JobRef):
    kind: Literal["accepted"]
    document_id: UUID


class UploadRejected(DTO):
    kind: Literal["rejected"]
    code: Literal["unknown_document", "invalid_upload", "too_large"]


UploadResult = Annotated[AcceptedUpload | UploadRejected, Field(discriminator="kind")]


class StagedUpload(DTO):
    path: Path
    source: UploadSource


class IngestionAttempt(JobRef):
    attempt_no: PositiveId
    deadline_at: AwareDatetime


class ClaimedIngestionJob(DTO):
    attempt: IngestionAttempt
    document_id: UUID
    staging_ref: NonEmptyText


class PreparedChunk(DTO):
    text: NonEmptyText
    locator: SourceLocator | None
~~~

`filename` — имя источника, которое Worker записывает как `source_name`. Для новой загрузки передаём `document_id=None`, для источника без URL — `source_url=None`. Файл открывается только по пути `StagedUpload.path`, полученному от адаптера тома. `job_id` равен `revision_id`. В Python это UUID, в [сообщении RabbitMQ](../infrastructure/rabbitmq/index.md#структура-сообщения) он сериализуется строкой.

`invalid_upload` означает пустой поток или некорректные входные данные. `too_large` означает превышение лимита при чтении потока. `unknown_document` возвращается для явно переданного неизвестного ID. При сбое записи или неизвестном коммите Gateway поднимает `StorageError`. Парсер поднимает `StepError` при сбое, например `unsupported_format` с `retryable=False`.

## Цепочка загрузки и публикации

| Кто вызывает | Что передаёт и откуда берёт | Что получает и использует дальше |
| --- | --- | --- |
| Gateway → [временный том](../infrastructure/upload-spool/contracts.md) | `stage_upload(revision_id, upload.content, upload.source)`. Gateway генерирует UUID редакции. | `staging_ref` после записи файла и метаданных либо отказ во входных данных. |
| Gateway → [PostgreSQL](../infrastructure/postgresql/ingestion.md#register-revision) | `register_revision(revision_id, upload.document_id, staging_ref)`. ID документа берём из входа, ссылку из тома. | `AcceptedUpload` после записи редакции и outbox. При неизвестном ID возвращается `None`, Gateway преобразует его в `unknown_document`. |
| [Queue Relay](../infrastructure/rabbitmq/contracts.md#relay-outbox-batch) → RabbitMQ → Worker | Relay берёт `revision_id` из outbox и отправляет его как `job_id`. Адаптер RabbitMQ преобразует строку в UUID и передаёт `JobRef` обработчику. | `ConsumeDecision` от `handle_ingestion_job` определяет подтверждение доставки. |
| Worker → PostgreSQL | `claim_ingestion_job(event.job_id)`. ID приходит из DTO `JobRef`. | `ClaimedIngestionJob` с данными редакции и попытки либо `None`, если сейчас брать нечего. |
| Worker → временный том и парсер | `open_staged_upload(job.staging_ref)`, затем `prepare_chunks(upload)`, если загрузка найдена. | Путь и метаданные из тома, затем упорядоченные тексты и локаторы. `None` от тома или пустой список от парсера завершают задание ошибкой без публикации. |
| Worker → [Model Gateway](../model-gateway/entrypoints.md#embed-texts) | `embed_texts(texts, "document", timeout_s=embedding_timeout_s)` получает тексты по порядку. Бюджет ограничен остатком срока задания. | `EmbeddingResult`: проверяем `profile_id`, берём по вектору из `vectors` на текст в том же порядке. Worker собирает точки из полей ниже. |
| Worker → PostgreSQL | `begin_revision_write(job.attempt, len(points))` перед отправкой точек. | `True` после сохранения числа чанков и признака незавершённой записи. Только этот исход разрешает отправку. |
| Worker → [Qdrant](../infrastructure/qdrant/contracts.md#write-revision-points) | `write_revision_points(job.attempt.job_id, points)` получает подготовленные точки. | `None` после подтверждения всех пакетов и проверки полного набора точек. |
| Worker → PostgreSQL | `publish_revision(job.attempt)` после готовности точек. | Новое поколение после коммита либо `None`, если попытка потеряла право на запись. Коммит одновременно снимает признак незавершённой записи и открывает редакцию для новых запросов. |
| Worker → том, Qdrant и PostgreSQL | `list_finished_staging` → `remove_staged_upload` → `clear_staging_ref`. Для индекса `claim_revision_cleanup` → `delete_revision_points` → `finish_revision_cleanup`. | Список файлов и список редакций определяют две независимые очереди уборки. Удаления идемпотентны, переходы в PostgreSQL выполняются после подтверждения удаления. |

В таблице показан успешный путь. Ветки сбоя описаны у [Gateway](./gateway.md#что-делать-при-сбое-приёма) и [Worker](./worker.md#повторы-и-подтверждение-очереди).

RAG Core получает номер поколения и ID видимых редакций через PostgreSQL. Searcher получает тексты и источники опубликованных точек через Qdrant. [Сквозной стык с RAG Core](../rag-core/flow.md#стык-с-ingestion)

## Сборка точки Qdrant

Worker собирает один [`ChunkPoint`](../infrastructure/qdrant/contracts.md#dto) на каждый `PreparedChunk`.

| Поле `ChunkPoint` | Источник |
| --- | --- |
| `document_id` | `job.document_id` из PostgreSQL. |
| `revision_id` | `job.attempt.job_id` из PostgreSQL, тот же ID, что в сообщении. |
| `chunk_no` | Индекс чанка в результате `prepare_chunks`, начиная с 0. |
| `chunk_id` | `uuid5(revision_id, str(chunk_no))`. При повторе получаем те же ID. |
| `text`, `locator` | Поля соответствующего `PreparedChunk`. |
| `source_name`, `source_url` | `upload.source.filename` и `upload.source.source_url` из манифеста. |
| `dense` | Вектор из `EmbeddingResult.vectors` на позиции `chunk_no`. |

Адаптер Qdrant сериализует точку. Текст для BM25 берётся из того же поля `text`. [Пример записи](../infrastructure/qdrant/index.md#структура-точки)
