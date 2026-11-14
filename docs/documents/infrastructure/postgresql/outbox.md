---
title: "Контракты PostgreSQL · Outbox и восстановление"
sidebar_position: 6
toc_max_heading_level: 2
---

Адаптер поднимает `StorageError` при недоступности PostgreSQL или неизвестном коммите. Время передаём в UTC. [Схема PostgreSQL](./index.md). [DTO RabbitMQ](../rabbitmq/contracts.md#dto).

## <span className="operation-badge operation-badge--db">DB</span> `list_ready_outbox` {#list-ready-outbox}

~~~python
async def list_ready_outbox(limit: int) -> list[ReadyOutboxRow]: ...
~~~

**Кто вызывает.** [Queue Relay](../queue-relay.md) перед публикацией.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `limit` | `int` | Положительный предел размера пачки. | `20` |

### Что отдаёт

**Формат.** Список объектов Pydantic `ReadyOutboxRow`. `[]` допустим.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `[].notice` | [`OutboxNotice`](../rabbitmq/contracts.md#dto) | ID, маршрут и событие для `publish_notice`. | `{"message_id": message_id, "route": "rag.questions", "event": {"request_id": 1042}}` |
| `[].available_at` | `datetime` | Сохранённое время готовности записи в UTC. Relay использует его для замера ожидания. | `datetime(2026, 11, 10, 12, 0, tzinfo=timezone.utc)` |

### Правила и ошибки

Возвращает неопубликованные записи, для которых наступил `available_at`.

## <span className="operation-badge operation-badge--db">DB</span> `mark_outbox_published` {#mark-outbox-published}

~~~python
async def mark_outbox_published(message_id: UUID) -> bool: ...
~~~

**Кто вызывает.** Queue Relay после подтверждения RabbitMQ.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `message_id` | `UUID` | ID из опубликованного OutboxNotice. | `UUID("581d505d-d09d-482b-aa88-11406ed5ae42")` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Время публикации зафиксировано. | `True` |
| `return` | `bool` | Запись отсутствует или уже отмечена. | `False` |



## <span className="operation-badge operation-badge--db">DB</span> `recover_pending_work` {#recover-pending-work}

~~~python
async def recover_pending_work(route: WorkRoute, limit: int) -> int: ...
~~~

**Кто вызывает.** Runtime при запуске и периодически, по поручению владельца этапа.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `route` | `WorkRoute` | Какой этап восстанавливаем. | `"ingestion.jobs"` |
| `limit` | `int` | Положительный предел размера пачки. | `20` |

### Что отдаёт

**Формат.** Одно целое число.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `int` | Число новых уведомлений для готовой незавершённой работы. | `2` |

### Правила и ошибки

Для rag.questions выбираем готовые вопросы, для rag.results — самостоятельные доставки по delivery_id, для ingestion.jobs — редакции. Выбираем готовую по порядку и сроку работу, включая истёкшие попытки. Для `ingestion.jobs` пропускаем редакции с `qdrant_write_pending=true` и следующие редакции того же документа. Проверку состояния и добавление записи выполняем согласованно с переходами владельца этапа. Если запись для маршрута и работы ещё ждёт публикации, вторую не добавляем.

Недавно опубликованное уведомление ещё может находиться в RabbitMQ. При периодической проверке даём ему окно на доставку и повторно уведомляем о той же работе только после этого окна. Возраст берём по последней публикации из outbox, в том числе после пересоздания очереди. Это ограничивает дубли при отставании потребителей. Повторная доставка безопасна благодаря проверке состояния при взятии работы.

## DTO

~~~python
from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText
from dataclasses import dataclass

class ReadyOutboxRow(DTO):
    notice: OutboxNotice
    available_at: AwareDatetime
~~~
