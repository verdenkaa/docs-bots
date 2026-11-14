---
title: "Контракты PostgreSQL · RAG Core"
sidebar_position: 3
toc_max_heading_level: 2
---

Все переходы сверяют processing, номер и действующий технический срок. Неизвестный коммит — StorageError. [DTO](../../rag-core/contracts.md).

## <span className="operation-badge operation-badge--db">DB</span> `claim_question` {#claim-question}

~~~python
async def claim_question(request_id: int) -> ClaimedQuestion | None: ...
~~~

**Кто вызывает.** Core из rag.questions.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| request_id | int | ID сохранённого вопроса. | 1042 |

### Что отдаёт

**Формат.** ClaimedQuestion или None.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | ClaimedQuestion | Попытка, input, received_at, prepared_text и поколение. | corpus_generation=42 |
| return | None | Завершён/занят/не готов либо отсутствует. | None |

### Правила и ошибки

Под блокировкой сотрудника берёт queued либо истёкшую processing-попытку. Других принятых вопросов этого сотрудника нет. При первом взятии закрепляет поколение, повтор его сохраняет. Номер сверх лимита допускается только для фиксации технического исхода.

## <span className="operation-badge operation-badge--db">DB</span> `save_prepared_question` {#save-prepared-question}

~~~python
async def save_prepared_question(attempt: ProcessingAttempt, text: str) -> bool: ...
~~~

**Кто вызывает.** Core после OCR, до Guardrails.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| attempt | ProcessingAttempt | Действующая попытка и объединённый OCR-текст/подпись. | 1042 |
| text | непустой str | Действующая попытка и объединённый OCR-текст/подпись. | "Ошибка AUTH-17" |

### Что отдаёт

**Формат.** bool.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | bool | Текст сохранён либо попытка устарела. | True |

### Правила и ошибки

Первый непустой prepared_text закрепляет результат OCR. Повтор использует его; изменение уже сохранённого текста — ошибка контракта. Пустой OCR без подписи не сохраняется как нормальный вопрос.

## <span className="operation-badge operation-badge--db">DB</span> `load_revision_ids` {#load-revision-ids}

~~~python
async def load_revision_ids(corpus_generation: int) -> frozenset[UUID]: ...
~~~

**Кто вызывает.** Core до Guardrails, затем тот же набор для Searcher.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| corpus_generation | int >= 0 | Закреплённый состав. | 42 |

### Что отдаёт

**Формат.** frozenset UUID.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | frozenset[UUID] | Видимые редакции; пустое множество допустимо. | frozenset() |

### Правила и ошибки

Интервал visible_from &lt;= generation &lt; visible_until; открытый верхний предел допустим. Закрепление защищает обе разновидности точек: чанки и примеры.

## <span className="operation-badge operation-badge--db">DB</span> `complete_question` {#complete-question}

~~~python
async def complete_question(attempt: ProcessingAttempt, outcome: QuestionOutcome) -> bool: ...
~~~

**Кто вызывает.** Core после бизнес-ветки.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| attempt | ProcessingAttempt | Право обработки. | request_id=1042 |
| outcome | QuestionOutcome | AnsweredOutcome / NoticeOutcome / SupportOutcome. | SupportOutcome(kind="support",reason="no_evidence") |

### Что отдаёт

**Формат.** bool.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | bool | Итог, освобождение поколения и следующий этап сохранены атомарно; False — устаревшая попытка. | True |

### Правила и ошибки

answered создаёт самостоятельную answer-доставку content=RagResult и outbox DeliveryRef. notice создаёт notice-доставку с текстом. support создаёт/находит case в creating и сохраняет намерение Support; немедленного повторного вопроса нет. Все ветки освобождают pinned_generation. Активный слот сотрудника остаётся до терминальной доставки. Очереди следующего вопроса этого сотрудника нет.

## <span className="operation-badge operation-badge--db">DB</span> `schedule_question_retry` {#schedule-question-retry}

~~~python
async def schedule_question_retry(attempt: ProcessingAttempt, retry_at: datetime) -> bool: ...
~~~

**Кто вызывает.** Core при временном сбое.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| attempt | ProcessingAttempt | Текущая попытка и следующий запуск. | 1042 |
| retry_at | UTC datetime | Текущая попытка и следующий запуск. | UTC |

### Что отдаёт

**Формат.** bool.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | bool | Повтор и outbox сохранены либо право утрачено. | True |

### Правила и ошибки

Возвращает queued, сохраняет поколение и prepared_text, записывает resume_at и RequestRef с available_at. received_at не меняется. Внешние вызовы идут вне транзакции.

Наличие принятого request уже подтверждает допуск на момент регистрации. Последующее enabled=False не запрещает claim/повтор/complete_question для этого request и не отменяет его модельные шаги. Сроки попыток, версия корпуса и ограничение одного активного вопроса сохраняются.
