---
title: "Точки входа RAG API"
sidebar_position: 2
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

[Общие DTO уведомления, попытки и результата](../../rag-core/contracts.md).

## <span className="operation-badge operation-badge--async">ASYNC</span> `handle_telegram_update` {#handle-telegram-update}

~~~python
async def handle_telegram_update(update: Mapping[str, object]) -> IntakeResult: ...
~~~

**Кто вызывает.** Webhook после проверки секрета или адаптер long polling.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `update` | `Mapping[str, object]` | Декодированный объект Telegram Update. | `{"update_id": 10001, "message": {...}}` |
| `update.update_id` | `int` | Дедупликация принятого обновления. | `10001` |
| `update.message.chat.id` | `int` | Адрес доставки и ключ порядка чата. Для текстового вопроса. | `123456789` |
| `update.message.text` | `str` | Исходный вопрос для регистрации и Guardrails. Для текстового вопроса. | `"Где инструкция по установке?"` |

### Что отдаёт

**Формат.** Один словарь Python `IntakeResult`. Только варианты `accepted` и `duplicate` содержат `request_id`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `kind` | `Literal["accepted", "duplicate"]` | Вопрос зарегистрирован или был принят раньше. | `"accepted"` |
| `request_id` | `int` | ID зарегистрированного вопроса. | `1042` |
| `kind` | `Literal["ignored", "invalid_update", "retry_later"]` | Обновление пропущено, некорректно или требует повтора приёма. | `"retry_later"` |

### Правила и ошибки

`ignored` означает корректное обновление вне сценария. Для текстового вопроса `register_question` возвращает прежний или новый ID. Если лимит достигнут или PostgreSQL вернул `StorageError`, обработчик возвращает `retry_later`. Для `invalid_update` webhook возвращает `400`, для `retry_later` — `503`. [Регистрация в PostgreSQL](../../infrastructure/postgresql/rag-api.md#register-question)

## <span className="operation-badge operation-badge--sub">SUB</span> `handle_rag_result` {#handle-rag-result}

~~~python
async def handle_rag_result(event: RequestRef) -> ConsumeDecision: ...
~~~

**Кто вызывает.** Адаптер RabbitMQ после проверки уведомления из `rag.results`.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `event` | `RequestRef` | Ссылка на строку запроса в PostgreSQL. | `{"request_id": 1042}` |
| `event.request_id` | `int` | ID для получения итога и активной попытки из claim_delivery. | `1042` |

### Что отдаёт

**Формат.** Одна строка Python, значение `ConsumeDecision`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `Literal["ack"]` | Разрешает подтвердить доставку после сохранённого перехода или если работа уже не требуется. | `"ack"` |
| `return` | `Literal["retry_broker"]` | Просит повторную доставку с задержкой, когда состояние PostgreSQL не удалось проверить или сохранить. | `"retry_broker"` |

### Правила и ошибки

При `None` от `claim_delivery` возвращает `ack`. Если попытка взята, вызывает `send_answer` и фиксирует исход или повтор. При исчерпании попыток только фиксирует итог. Неизвестное состояние или коммит дают `retry_broker`. [Доставка и повторы](./queue-processing.md#отправка-результата)

## <span className="operation-badge operation-badge--async">ASYNC</span> `send_answer` {#send-answer}

~~~python
async def send_answer(chat_id: int, result: RagResult) -> SendResult: ...
~~~

**Кто вызывает.** `handle_rag_result` после взятия попытки доставки.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `chat_id` | `int` | Чат из ClaimedDelivery. | `123456789` |
| `result` | `RagResult` | Сохранённые текст и список источников из ClaimedDelivery. | `{"answer_text": "См. раздел «Установка».", "sources": [...]}` |

### Что отдаёт

**Формат.** Один словарь Python `SendResult`. Дополнительные поля есть только для `failed`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `kind` | `Literal["sent"]` | Telegram подтвердил отправку ответа или резервного текста. | `"sent"` |
| `kind` | `Literal["failed"]` | Отправка не подтверждена. | `"failed"` |
| `retryable` | `bool` | Можно ли повторить отправку. | `True` |
| `uncertain` | `bool` | Могло ли сообщение дойти без подтверждения. | `True` |
| `retry_after_seconds` | `int \| None` | Задержка, если её вернул Telegram. | `None` |

### Правила и ошибки

Доставка рассчитана на одно сообщение. Telegram ограничивает видимый текст 4096 символами. Для локальной проверки используем консервативный предел в 4096 единиц UTF-16 и учитываем весь видимый текст, включая переводы строк. Источникам выделяем до 512 единиц вместе с заголовком блока. Это оставляет 3584 единицы для текста при полном блоке. Если места меньше, сокращаем названия, убираем необязательные детали и оставляем первые источники, которые помещаются. Для ответа по документам сохраняем хотя бы одну ссылку или краткое название источника. [Telegram sendMessage](https://core.telegram.org/bots/api#sendmessage)

Если целый ответ с минимальным оформлением всё ещё не помещается, в той же попытке отправляем обычным текстом без источников: «Ответ не помещается в одно сообщение. Пожалуйста, задайте более узкий вопрос.» Если Telegram явно отклонил исходное сообщение из-за длины, один раз отправляем этот же резервный текст в пределах срока и лимита частоты текущей попытки. При таймауте исходного вызова применяем обычные правила неизвестного исхода. По ответу на резервный текст возвращаем тот же `SendResult`: подтверждение даёт `sent`, временный сбой или отказ обрабатывается по общим правилам. В логе отмечаем `delivery_variant="length_fallback"`.

## DTO

~~~python
from collections.abc import Mapping
from typing import Literal, TypedDict


class RegisteredQuestion(TypedDict):
    request_id: int
    created: bool


class RecordedUpdate(TypedDict):
    kind: Literal["accepted", "duplicate"]
    request_id: int


class UnrecordedUpdate(TypedDict):
    kind: Literal["ignored", "invalid_update", "retry_later"]


IntakeResult = RecordedUpdate | UnrecordedUpdate


class ClaimedDelivery(TypedDict):
    attempt: ProcessingAttempt
    chat_id: int
    result: RagResult


class Sent(TypedDict):
    kind: Literal["sent"]


class SendFailed(TypedDict):
    kind: Literal["failed"]
    retryable: bool
    uncertain: bool
    retry_after_seconds: int | None


SendResult = Sent | SendFailed
~~~
