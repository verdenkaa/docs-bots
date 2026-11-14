---
title: "Точки входа Ingestion"
sidebar_position: 4
toc_max_heading_level: 2
---

[DTO Ingestion](./contracts.md) · [Gateway](./gateway.md) · [Worker](./worker.md)

## <span className="operation-badge operation-badge--async">ASYNC</span> `accept_upload` {#accept-upload}

~~~python
async def accept_upload(upload: UploadInput) -> UploadResult: ...
~~~

**Кто вызывает.** Адаптер после проверки доступа.

### Что принимает

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `upload` | UploadInput | Поток и метаданные. | UploadInput(...) |
| upload.content | AsyncIterable[bytes] | Оригинал текущего вызова. | file_stream |
| upload.source.filename | str | Название источника. | Руководство.docx |
| upload.document_id | UUID или None | Замена либо новый документ. | None |

### Что отдаёт

**Формат.** Один вариант Pydantic UploadResult.

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| kind | accepted / rejected | Надёжный приём / отказ. | accepted |
| document_id / job_id | UUID / UUID | Документ / редакция; только accepted. | UUID(...) |
| code | str | Только rejected; коды в DTO. | unsupported_format |

### Правила и ошибки

Конвертация/S3/Markdown выполняются до accepted. StepError/StorageError — технический сбой; неизвестный коммит не равен rejected. Новый вызов не обещает дедупликацию внешнего транспорта.

## <span className="operation-badge operation-badge--async">ASYNC</span> `convert_document` {#convert-document}

~~~python
async def convert_document(upload: UploadInput, revision_id: UUID) -> ConversionResult: ...
~~~

**Кто вызывает.** Gateway до записи S3/тома/задания.

### Что принимает

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| upload | UploadInput | Поток читается один раз с лимитами. | UploadInput(...) |
| revision_id | UUID | Основа стабильных ID изображений. | UUID(...) |

### Что отдаёт

**Формат.** Один Pydantic ConversionResult, без оригинала.

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| markdown | NonEmptyText | Markdown с неделимыми плейсхолдерами. | `![Схема](image://…)` |
| images | list[ConvertedImage] | Изображения в порядке документа, [] допустим. | [] |
| images[].ref | ImageRef | UUID и OCR/VLM-описание. | ImageRef(...) |
| images[].content | ImageContent | Проверенные байты и MIME для put_image. | ImageContent(...) |

### Правила и ошибки

Не пишет оригинал или S3. Окончательный отказ разбора — StepError(unsupported_format/conversion_failed, False), лимит — StepError(too_large, False); Gateway переводит их в UploadRejected. Сбой OCR/VLM остаётся техническим. [Порядок](./converter.md).

## <span className="operation-badge operation-badge--sub">SUB</span> `handle_ingestion_job` {#handle-ingestion-job}

~~~python
async def handle_ingestion_job(event: JobRef) -> ConsumeDecision: ...
~~~

**Кто вызывает.** RabbitMQ-адаптер ingestion.jobs.

### Что принимает

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| event | JobRef | Ссылка на задание, не файл. | JobRef(...) |
| event.job_id | UUID | Ключ claim_ingestion_job. | UUID(...) |

### Что отдаёт

**Формат.** Одна строка ConsumeDecision.

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| return | Literal["ack"] | Переход сохранён либо БД подтвердила отсутствие доступной работы. | ack |
| return | Literal["retry_broker"] | Состояние/коммит PostgreSQL неизвестен. | retry_broker |

### Правила и ошибки

Читает готовый MD и подготовленный набор. Публикует оба индекса. Неизвестная запись Qdrant блокирует повтор/удаление до сверки. [Все исходы](./worker.md#повторы-и-подтверждение-очереди).

## <span className="operation-badge operation-badge--async">ASYNC</span> `prepare_chunks` {#prepare-chunks}

~~~python
async def prepare_chunks(upload: StagedUpload) -> list[PreparedChunk]: ...
~~~

**Кто вызывает.** Worker, если PreparedRevision ещё не сохранён.

### Что принимает

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| upload | StagedUpload | Markdown и манифест из Runtime. | StagedUpload(...) |
| upload.path | Path | Только converted.md от адаптера тома. | Path(".../converted.md") |
| upload.source | UploadSource | Название исходника. | Руководство.docx |
| upload.images | list[ImageRef] | Разрешённые плейсхолдеры. | [] |

### Что отдаёт

**Формат.** Упорядоченный список PreparedChunk; [] — нет пригодного содержимого.

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| [].text | NonEmptyText | Текст и описания для индекса. | Нажмите фильтр… |
| [].locator | SourceLocator или None | Внутренняя диагностика, не обязательная ссылка. | None |
| [].images | list[ImageRef] | Изображения именно этого чанка. | [] |

### Правила и ошибки

Первично режет по разделам, крупные части — с настраиваемым перекрытием. Не разрывает плейсхолдер; неделимый блок сверх бюджета — StepError(chunk_too_large, False). Не выполняет OCR/VLM/конвертацию. Неизвестный ID — ошибка. После сохранения PreparedRevision повтор использует снимок.
