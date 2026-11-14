---
title: "Telegram-медиа и метаданные источников"
sidebar_position: 8
toc_max_heading_level: 2
---

~~~python
class SourceMetadata(DTO):
    document_id: UUID
    source_name: NonEmptyText
~~~

| Поле | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| data | bytes | Байты разрешённого изображения, только в памяти текущего вызова. | `b"…"` |
| mime_type | str | Проверенный формат; не доверяем лишь имени файла. | `"image/png"` |
| document_id | UUID | Источник, известный Ingestion. | UUID |
| source_name | str | Сохранённое название, экранируется при HTML-выводе. | `"Реестр мебели"` |

ImageContent определён в [общих DTO](../rag-core/contracts.md#общие-dto), SourceMetadata наследует общий DTO; data не сериализуется в RabbitMQ/логи. Хранение изображений документов выделено в отдельный [адаптер S3](./s3.md) (`rag.runtime.s3`): `put_image` для Ingestion и `get_image` для доставки RAG API.

## <span className="operation-badge operation-badge--async">ASYNC</span> `get_question_image` {#get-question-image}

~~~python
async def get_question_image(file_id: str) -> ImageContent: ...
~~~

**Кто вызывает.** Core для ImageQuestion до OCR.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| file_id | str | Telegram file_id из сохранённого входа. | AgAC... |

### Что отдаёт

**Формат.** ImageContent.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| data / mime_type | bytes / str | Проверенное содержимое входящего изображения. | b"…" / "image/png" |

### Правила и ошибки

Telegram-адаптер Runtime использует токен из конфигурации, выполняет getFile/скачивание с лимитом размера и времени. Нельзя передать произвольный URL. Временный сбой — StepError(retryable=True), окончательная недоступность — StepError(retryable=False). Повтор после успешного OCR использует prepared_text и не обязан скачивать файл снова.

## <span className="operation-badge operation-badge--db">DB</span> `load_source_metadata` {#load-source-metadata}

~~~python
async def load_source_metadata(document_id: UUID) -> SourceMetadata | None: ...
~~~

**Кто вызывает.** RAG API при открытии HTML-заглушки.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| document_id | UUID | Из параметра пути источника. | UUID(...) |

### Что отдаёт

**Формат.** SourceMetadata или None.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | SourceMetadata или None | Название из сохранённой опубликованной редакции; либо неизвестный документ. | SourceMetadata(...) |

### Правила и ошибки

Читает PostgreSQL, не Qdrant и не оригинал. Очистка старых точек не удаляет метаданные источника. Адрес страницы строит RAG API, название не берётся из произвольной query-строки.
