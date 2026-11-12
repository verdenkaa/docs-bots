---
title: "Контракты PostgreSQL · RAG API"
sidebar_position: 2
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

Адаптер поднимает `StorageError` при недоступности PostgreSQL или неизвестном коммите. Время передаём в UTC. [Схема PostgreSQL](./index.md). [DTO RAG API](../../telegram/module/entrypoints.md#dto).

## <span className="operation-badge operation-badge--db">DB</span> `register_question` {#register-question}

~~~python
async def register_question(update_id: int, chat_id: int, question_text: str) -> RegisteredQuestion | None: ...
~~~

**Кто вызывает.** RAG API.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `update_id` | `int` | Telegram Update ID для дедупликации. | `10001` |
| `chat_id` | `int` | Чат для доставки и порядок вопросов. | `123456789` |
| `question_text` | `str` | Исходный текст Telegram Update, который позже получит Guardrails. | `"Где инструкция по установке?"` |

### Что отдаёт

**Формат.** Объект Pydantic `RegisteredQuestion` или `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `request_id` | `int` | ID принятого вопроса. Для повтора возвращается прежний. | `1042` |
| `created` | `bool` | True для новой записи и False для дубликата. | `True` |
| `return` | `None` | Достигнут лимит принятых запросов, по которым доставка ещё не завершена. | `None` |

### Правила и ошибки

Проверку `update_id`, проверку лимита и вставку согласует между всеми процессами приёма. Уже принятый вопрос возвращает прежний ID даже при достигнутом лимите. Новый запрос и outbox для `rag.questions` фиксирует одной транзакцией. [Контракт RAG API](../../telegram/module/entrypoints.md#handle-telegram-update)

## <span className="operation-badge operation-badge--db">DB</span> `claim_delivery` {#claim-delivery}

~~~python
async def claim_delivery(request_id: int) -> ClaimedDelivery | None: ...
~~~

**Кто вызывает.** RAG API.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `request_id` | `int` | Ссылка из уведомления RabbitMQ на сохранённый запрос. | `1042` |

### Что отдаёт

**Формат.** Объект Pydantic `ClaimedDelivery` или `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `attempt` | `ProcessingAttempt` | Номер и срок активной попытки доставки. | `{"request_id": 1042, "attempt_no": 1, "deadline_at": ...}` |
| `chat_id` | `int` | Чат, в который отправляем результат. | `123456789` |
| `result` | `RagResult` | Сохранённый итог для send_answer. | `{"answer_text": "См. раздел «Установка».", "sources": [...]}` |
| `return` | `None` | В момент вызова готовой доставки нет. | `None` |

### Правила и ошибки

Под блокировкой чата проверяет порядок и берёт готовый по времени `ready_to_deliver` или `delivering` с истёкшим сроком. Увеличивает `attempt_no`, ставит `delivering` и записывает срок в `resume_at`. При взятии истёкшей попытки выставляет `delivery_uncertain`, потому что отправка могла успеть дойти. Если номер новой попытки выше лимита доставки, RAG API использует её только для фиксации итога. Если строки запроса нет, адаптер фиксирует диагностическое событие перед возвратом `None`.

## <span className="operation-badge operation-badge--db">DB</span> `schedule_delivery_retry` {#schedule-delivery-retry}

~~~python
async def schedule_delivery_retry(attempt: ProcessingAttempt, retry_at: datetime, uncertain: bool) -> bool: ...
~~~

**Кто вызывает.** RAG API.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `attempt` | `ProcessingAttempt` | Активная попытка, полученная при взятии работы. | `{"request_id": 1042, "attempt_no": 1, "deadline_at": ...}` |
| `retry_at` | `datetime` | Время, с которого можно повторить работу. Дата и время в UTC. | `datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)` |
| `uncertain` | `bool` | Могла ли текущая отправка дойти без подтверждения. | `True` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Состояние ожидания и outbox для rag.results сохранены. | `True` |
| `return` | `bool` | Переход не выполнен, например попытка потеряла право на запись. | `False` |

### Правила и ошибки

Сверяет `delivering`, номер и срок попытки. Возвращает запрос в `ready_to_deliver`, сохраняет `retry_at` в `resume_at` и добавляет outbox с тем же `available_at`. Признак неоднозначности накапливается по всем попыткам доставки.

## <span className="operation-badge operation-badge--db">DB</span> `complete_delivery` {#complete-delivery}

~~~python
async def complete_delivery(attempt: ProcessingAttempt, delivered: bool, uncertain: bool) -> bool: ...
~~~

**Кто вызывает.** RAG API.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `attempt` | `ProcessingAttempt` | Активная попытка, полученная при взятии работы. | `{"request_id": 1042, "attempt_no": 1, "deadline_at": ...}` |
| `delivered` | `bool` | Подтвердил ли Telegram отправку. | `False` |
| `uncertain` | `bool` | Могла ли текущая отправка дойти без подтверждения. | `True` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Терминальное состояние сохранено. Следующая готовая доставка поставлена в outbox, если она есть. | `True` |
| `return` | `bool` | Переход не выполнен, например попытка потеряла право на запись. | `False` |

### Правила и ошибки

Сверяет `delivering`, номер и срок попытки. Подтверждённая отправка даёт `delivered`. Если подтверждения нет, учитывает сохранённый `delivery_uncertain` и текущий `uncertain`. При любой неоднозначности сохраняет `delivery_unknown`, иначе `delivery_failed`. Следующая готовая доставка ставится под той же блокировкой чата.
