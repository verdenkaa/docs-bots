---
title: "Контракты PostgreSQL · RAG Core"
sidebar_position: 3
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

Адаптер поднимает `StorageError` при недоступности PostgreSQL или неизвестном коммите. Время передаём в UTC. [Схема PostgreSQL](./index.md). [DTO RAG Core](../../rag-core/contracts.md#общие-dto).

## <span className="operation-badge operation-badge--db">DB</span> `claim_question` {#claim-question}

~~~python
async def claim_question(request_id: int) -> ClaimedQuestion | None: ...
~~~

**Кто вызывает.** RAG Core.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `request_id` | `int` | Ссылка из уведомления RabbitMQ на сохранённый запрос. | `1042` |

### Что отдаёт

**Формат.** Объект Pydantic `ClaimedQuestion` или `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `attempt` | `ProcessingAttempt` | ID, номер и срок активной попытки. | `{"request_id": 1042, "attempt_no": 1, "deadline_at": ...}` |
| `raw_question` | `str` | Исходный question_text для Guardrails. | `"Где инструкция по установке?"` |
| `corpus_generation` | `int` | Поколение из pinned_generation для кеша и поиска. | `42` |
| `return` | `None` | Запрос отсутствует, уже завершён, занят или пока не готов по порядку и времени. | `None` |

### Правила и ошибки

Под блокировкой чата берёт готовый по времени `queued` или `processing` с истёкшим сроком, если запрос первый на этапе обработки в этом чате. Ставит `processing`, увеличивает `attempt_no` и сохраняет срок в `resume_at`. При первом взятии закрепляет поколение, при повторе использует сохранённое. Если номер попытки выше лимита обработки, Core использует её только для фиксации технического итога. [Блокировки и повторы](../../rag-core/flow.md#транзакции-и-повторы)

## <span className="operation-badge operation-badge--db">DB</span> `load_revision_ids` {#load-revision-ids}

~~~python
async def load_revision_ids(corpus_generation: int) -> frozenset[UUID]: ...
~~~

**Кто вызывает.** RAG Core.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `corpus_generation` | `int` | Поколение, закреплённое в PostgreSQL за этим запросом. | `42` |

### Что отдаёт

**Формат.** Неизменяемое множество UUID. Пустое множество допустимо.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `frozenset[UUID]` | ID редакций, чьи интервалы видимости содержат указанное поколение. | `frozenset({UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")})` |

### Правила и ошибки

Включает редакцию, если нижняя граница видимости задана и не выше поколения, а верхняя граница отсутствует или выше него.

## <span className="operation-badge operation-badge--db">DB</span> `complete_question` {#complete-question}

~~~python
async def complete_question(attempt: ProcessingAttempt, result: RagResult) -> bool: ...
~~~

**Кто вызывает.** RAG Core.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `attempt` | `ProcessingAttempt` | Активная попытка, полученная при взятии работы. | `{"request_id": 1042, "attempt_no": 1, "deadline_at": ...}` |
| `result` | `RagResult` | Готовый текст и источники. | `{"answer_text": "См. раздел «Установка».", "sources": [...]}` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Результат, освобождение закрепления и уведомление rag.results сохранены одной транзакцией. | `True` |
| `return` | `bool` | Переход не выполнен, например попытка потеряла право на запись. | `False` |

### Правила и ошибки

Сверяет `processing`, номер и срок попытки. При коммите ставит `ready_to_deliver`, обнуляет счётчик попыток для нового этапа и очищает срок. В той же транзакции уведомляет о следующем готовом вопросе чата.

## <span className="operation-badge operation-badge--db">DB</span> `schedule_question_retry` {#schedule-question-retry}

~~~python
async def schedule_question_retry(attempt: ProcessingAttempt, retry_at: datetime) -> bool: ...
~~~

**Кто вызывает.** RAG Core.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `attempt` | `ProcessingAttempt` | Активная попытка, полученная при взятии работы. | `{"request_id": 1042, "attempt_no": 1, "deadline_at": ...}` |
| `retry_at` | `datetime` | Время, с которого можно повторить работу. Дата и время в UTC. | `datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Ожидание и outbox для rag.questions сохранены. Поколение остаётся закреплённым. | `True` |
| `return` | `bool` | Переход не выполнен, например попытка потеряла право на запись. | `False` |

### Правила и ошибки

Сверяет `processing`, номер и срок попытки. Ставит `queued`, сохраняет `retry_at` в `resume_at` и создаёт outbox с тем же `available_at`.
