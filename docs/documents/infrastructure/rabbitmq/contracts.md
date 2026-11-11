---
title: "Контракты RabbitMQ и Queue Relay"
sidebar_position: 2
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

[Правила DTO и Pydantic](../../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.

[Формат сообщений](./index.md#структура-сообщения) · [Функции outbox](../postgresql/outbox.md).

## <span className="operation-badge operation-badge--task">TASK</span> `relay_outbox_batch` {#relay-outbox-batch}

~~~python
async def relay_outbox_batch(limit: int) -> int: ...
~~~

**Кто вызывает.** Runtime периодически, в одном активном Queue Relay.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `limit` | `int` | Положительный предел размера пачки. | `20` |

### Что отдаёт

**Формат.** Одно целое число.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `int` | Сколько уведомлений подтверждено брокером и отмечено в PostgreSQL. | `3` |

### Правила и ошибки

Получает `ReadyOutboxRow` через `list_ready_outbox`. Для каждой строки передаёт `row.notice` в `publish_notice`, затем вызывает `mark_outbox_published`. `row.available_at` использует для диагностики ожидания в outbox. При сбое текущая запись остаётся доступной для повторной обработки.

## <span className="operation-badge operation-badge--task">TASK</span> `publish_notice` {#publish-notice}

~~~python
async def publish_notice(notice: OutboxNotice) -> None: ...
~~~

**Кто вызывает.** Queue Relay для очередной записи outbox.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `notice` | `OutboxNotice` | Уведомление из поля `notice` записи `ReadyOutboxRow`. | `{"message_id": UUID("581d505d-d09d-482b-aa88-11406ed5ae42"), "route": "rag.questions", "event": {"request_id": 1042}}` |
| `notice.message_id` | `UUID` | Стабильный AMQP message_id. | `UUID("581d505d-d09d-482b-aa88-11406ed5ae42")` |
| `notice.route` | `WorkRoute` | Ключ маршрутизации уведомления. | `"rag.questions"` |
| `notice.event` | `RequestRef \| JobRef` | Данные тела сообщения. Тип определяется маршрутом. | `{"request_id": 1042}` |

### Что отдаёт

**Формат.** Одно значение Python, `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `None` | Брокер подтвердил публикацию, сообщение успешно маршрутизировано. | `None` |

### Правила и ошибки

При сбое, неизвестном результате или немаршрутизированном сообщении поднимает `BrokerPublishError`.

## Доставка уведомлений

Адаптер проверяет DTO, вызывает обработчик из таблицы и применяет его `ConsumeDecision`. Некорректное событие фиксирует и подтверждает сам адаптер.

Для `ack` подтверждает доставку. Для `retry_broker` оставляет сообщение неподтверждённым на время ограниченной задержки, затем возвращает его в очередь. При разрыве соединения возврат выполняет брокер. Размер prefetch ограничивает число таких сообщений, а задержку настраиваем ниже таймаута подтверждения потребителя. Прикладные повторы после коммита идут через outbox и получают обычный `ack`. [Подтверждения RabbitMQ](https://www.rabbitmq.com/docs/confirms)

| Очередь | Обработчик |
| --- | --- |
| `rag.questions` | <span className="operation-badge operation-badge--sub">SUB</span> [`handle_rag_question`](../../rag-core/entrypoints.md#handle-rag-question) |
| `rag.results` | <span className="operation-badge operation-badge--sub">SUB</span> [`handle_rag_result`](../../telegram/module/entrypoints.md#handle-rag-result) |
| `ingestion.jobs` | <span className="operation-badge operation-badge--sub">SUB</span> [`handle_ingestion_job`](../../ingestion/entrypoints.md#handle-ingestion-job) |

## DTO

~~~python
from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText
from dataclasses import dataclass

WorkRoute = Literal["rag.questions", "rag.results", "ingestion.jobs"]


class RagNotice(DTO):
    message_id: UUID
    route: Literal["rag.questions", "rag.results"]
    event: RequestRef


class IngestionNotice(DTO):
    message_id: UUID
    route: Literal["ingestion.jobs"]
    event: JobRef


OutboxNotice = Annotated[RagNotice | IngestionNotice, Field(discriminator="route")]
~~~

[DTO RAG](../../rag-core/contracts.md#очередь-и-готовый-результат) · [DTO Ingestion](../../ingestion/contracts.md#dto).
