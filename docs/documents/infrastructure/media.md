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

## <span className="operation-badge operation-badge--async">ASYNC</span> `prepare_telegram_photo` {#prepare-telegram-photo}

~~~python
async def prepare_telegram_photo(image: ImageContent) -> ImageContent:
    ...
~~~

**Кто вызывает.** RAG API.send_answer: get_image → prepare_telegram_photo → sendPhoto.

### Что принимает

| Параметр | Тип | Смысл |
| --- | --- | --- |
| image | ImageContent | Байты и проверенный MIME из S3. |

### Что отдаёт

ImageContent с MIME image/jpeg; это подготовленная копия, объект S3 и image_id не меняются.

### Правила и ошибки

Runtime декодирует изображение с лимитом памяти/пикселей, применяет ориентацию, переводит в RGB (прозрачность на белом фоне) и JPEG. Содержимое не обрезает: чрезмерно узкое/длинное изображение дополняет полями до отношения сторон не более 20; затем пропорционально уменьшает так, чтобы сумма ширины и высоты была не более 10000. Подбирает качество JPEG и при необходимости уменьшает размеры до объёма не более 10 000 000 байт. Итог повторно проверяется. Политика преобразования фиксируется конфигурацией Runtime; sendDocument не используется.

CPU-работа выполняется вне event loop управляемым исполнителем в пределах срока попытки доставки. Неподдерживаемый/повреждённый или непреобразуемый вход — StepError(telegram_photo_invalid, False); временный отказ исполнителя — StepError с retryable=True. До успешного преобразования Telegram-вызова нет и uncertain=False. send_answer переводит эти ошибки в SendFailed; ранее подтверждённый текст не отправляется повторно. Байты копии существуют только в текущем вызове; повтор читает тот же объект S3 и применяет тот же профиль.

[Ограничения Telegram sendPhoto](https://core.telegram.org/bots/api#sendphoto).
