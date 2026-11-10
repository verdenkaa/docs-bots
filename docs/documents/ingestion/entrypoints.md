---
title: "Точки входа Ingestion"
sidebar_position: 4
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Способ подключения адаптера, форматы документов и лимиты уточним по кейсу.
:::

[Приём файла](./gateway.md) · [Обработка задания](./worker.md) · [DTO](./contracts.md#dto)

## <span className="operation-badge operation-badge--async">ASYNC</span> `accept_upload` {#accept-upload}

~~~python
async def accept_upload(upload: UploadInput) -> UploadResult: ...
~~~

**Кто вызывает.** Адаптер загрузки после проверки доступа.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `upload` | `UploadInput` | Объект загрузки с потоком байтов и метаданными. | `{"content": file_stream, "source": {...}, "document_id": None}` |
| `upload.content` | `AsyncIterable[bytes]` | Поток файла для записи во временный том. | `file_stream` |
| `upload.source.filename` | `str` | Имя файла для определения формата и подписи источника. | `"Руководство по установке.pdf"` |
| `upload.source.source_url` | `str \| None` | Адрес источника для ссылки в ответе, если он известен. | `None` |
| `upload.document_id` | `UUID \| None` | Существующий документ при замене. `None` для нового документа. | `None` |

### Что отдаёт

**Формат.** Один словарь Python `UploadResult`. Поля зависят от `kind`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `kind` | `Literal["accepted"]` | Файл, задание и outbox зафиксированы. Индексация выполняется отдельно. | `"accepted"` |
| `document_id` | `UUID` | ID документа, который можно передать при следующем обновлении. | `UUID("43f95f66-7343-4cbc-beb5-19fd36a8ca77")` |
| `job_id` | `UUID` | ID задания. Равен revision_id новой редакции. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `kind` | `Literal["rejected"]` | Загрузка отклонена. | `"rejected"` |
| `code` | `Literal["unknown_document", "invalid_upload", "too_large"]` | Причина отказа. Поле есть только в варианте rejected. | `"unknown_document"` |

### Правила и ошибки

При техническом сбое записи или неизвестном коммите поднимает `StorageError`. Каждая новая загрузка получает свой UUID редакции. [Повторы приёма](./gateway.md#что-делать-при-сбое-приёма)

## <span className="operation-badge operation-badge--sub">SUB</span> `handle_ingestion_job` {#handle-ingestion-job}

~~~python
async def handle_ingestion_job(event: JobRef) -> ConsumeDecision: ...
~~~

**Кто вызывает.** Адаптер RabbitMQ для `ingestion.jobs`.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `event` | `JobRef` | Ссылка на сохранённое задание. | `{"job_id": UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")}` |
| `event.job_id` | `UUID` | ID редакции для claim_ingestion_job. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |

### Что отдаёт

**Формат.** Одна строка Python, значение `ConsumeDecision`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `Literal["ack"]` | Разрешает подтвердить доставку после сохранённого перехода или если работа уже не требуется. | `"ack"` |
| `return` | `Literal["retry_broker"]` | Просит повторную доставку с задержкой, когда состояние PostgreSQL не удалось проверить или сохранить. | `"retry_broker"` |

### Правила и ошибки

Публикация, фиксация ошибки и план повтора считаются сохранёнными переходами. `ack` также возвращаем, если PostgreSQL подтвердил, что по этому уведомлению сейчас работы нет, в том числе когда редакция ждёт сверки записи. При неизвестном состоянии или коммите возвращаем `retry_broker`. Если супервизор остановил процесс, RabbitMQ вернёт неподтверждённое уведомление при закрытии соединения. [Таблица исходов](./worker.md#повторы-и-подтверждение-очереди)

## <span className="operation-badge operation-badge--async">ASYNC</span> `prepare_chunks` {#prepare-chunks}

~~~python
async def prepare_chunks(upload: StagedUpload) -> list[PreparedChunk]: ...
~~~

**Кто вызывает.** Ingestion Worker после чтения файла из рабочего тома.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `upload` | `StagedUpload` | Путь к сохранённому файлу и его метаданные. | `{"path": Path(".../source"), "source": {...}}` |
| `upload.path` | `Path` | Локальный путь для чтения парсером. | `Path(".../source")` |
| `upload.source` | `UploadSource` | Имя файла и адрес, прочитанные из манифеста. | `{"filename": "Руководство по установке.pdf", "source_url": None}` |

### Что отдаёт

**Формат.** Упорядоченный список словарей `PreparedChunk`. `[]` означает отсутствие пригодного текста.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `[].text` | `str` | Текст чанка для векторизации и поиска. | `"Фрагмент инструкции по установке."` |
| `[].locator` | `SourceLocator \| None` | Место чанка в исходном документе, если его можно определить. | `{"page": 4, "section": "Установка"}` |

### Правила и ошибки

При ошибке парсинга поднимает `StepError(code, retryable)`. С тем же файлом и профилем возвращает чанки в том же порядке. [Правила подготовки текста](./worker.md#подготовка-текста)
