---
title: "Контракты PostgreSQL · доступ и поддержка"
sidebar_position: 2.5
toc_max_heading_level: 2
---

[DTO](../../telegram/module/contracts.md). Все мутации идемпотентны и проверяют права/состояние внутри транзакции. StorageError — неизвестный коммит; AccessDenied, NotFound, Conflict — известный бизнес-отказ.

## <span className="operation-badge operation-badge--db">DB</span> `read_user_access` {#read-user-access}

~~~python
async def read_user_access(user_id: int) -> AccessRecord | None: ...
~~~

**Кто вызывает.** check_access.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| user_id | int | ID отправителя. | 123 |

### Что отдаёт

**Формат.** AccessRecord или None.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | AccessRecord или None | Запись допуска, включая enabled/role. | AccessRecord(...) |

### Правила и ошибки

Чтение не меняет допуск.

## <span className="operation-badge operation-badge--db">DB</span> `write_user_access` {#write-user-access}

~~~python
async def write_user_access(actor: Principal, record: AccessRecord) -> AccessRecord: ...
~~~

**Кто вызывает.** set_user_access.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Проверенный admin. | role="admin" |
| record | AccessRecord | Новое состояние сотрудника. | enabled=False |

### Что отдаёт

**Формат.** AccessRecord.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | AccessRecord | Сохранённая запись. | AccessRecord(...) |

### Правила и ошибки

Внутри транзакции повторно проверяет актуальный enabled/admin у actor. Одинаковое повторное изменение безопасно; входящий Principal не является заменой проверки текущего допуска. Изменение target-user сериализуется с register_question по user_id. enabled=False запрещает новые вопросы, но не отменяет ранее принятый request, его доставку или автоматическое создание тикета.

## <span className="operation-badge operation-badge--db">DB</span> `record_support_action` {#record-support-action}

~~~python
async def record_support_action(command: SupportCommand, check_at: datetime | None) -> IntakeResult: ...
~~~

**Кто вызывает.** handle_support_action.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| command | SupportCommand | Update, сотрудник и действие. | action.kind="start" |
| check_at | UTC datetime или None | Для start задаётся RAG API как received_at+10 минут, иначе None. | UTC |

### Что отдаёт

**Формат.** IntakeResult.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | IntakeResult | Прежнее действие либо сохранённые case/доставка. | kind="handled" |

### Правила и ошибки

Одна транзакция: ledger update, проверка enabled/владельца/стадии и переход. offer требует AnsweredOutcome и подтверждённую текстовую часть (normal или length_fallback), не весь набор картинок; иначе создаёт first_question-уведомление. start находит сохранённую причину offer, создаёт один waiting-case и первый срок. confirm применим только к awaiting_confirmation и ставит creating; decline ставит cancelled. Повтор не сдвигает таймер. Ссылки callback сверяются с Telegram message_id и владельцем, чужая/устаревшая команда не меняет case.

## <span className="operation-badge operation-badge--db">DB</span> `list_pending_support` {#list-pending-support}

~~~python
async def list_pending_support(now: datetime, limit: int) -> list[SupportCase]: ...
~~~

**Кто вызывает.** run_support_batch.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| now | UTC datetime | Готовность ожиданий. | UTC |
| limit | int > 0 | Предел пачки. | 20 |

### Что отдаёт

**Формат.** Список SupportCase.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | list[SupportCase] | waiting с наступившим check_at либо creating. | [] |

### Правила и ошибки

Несколько читателей допустимы: последующие операции проверяют ожидаемое состояние атомарно.

## <span className="operation-badge operation-badge--db">DB</span> `queue_support_confirmation` {#queue-support-confirmation}

~~~python
async def queue_support_confirmation(case_id: UUID, now: datetime) -> bool: ...
~~~

**Кто вызывает.** run_support_batch.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| case_id | UUID | Истёкшее ожидание. | UUID(...) |
| now | UTC datetime | Проверка наступления срока. | UTC |

### Что отдаёт

**Формат.** bool.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | bool | waiting переведён в awaiting_confirmation с доставкой либо переход не нужен. | True |

### Правила и ошибки

Проверяет check_at &lt;= now, создаёт одну support_confirmation-доставку и outbox. Повтор не создаёт вторую. Закрытый/созданный case не возобновляется.

## <span className="operation-badge operation-badge--db">DB</span> `create_support_ticket` {#create-support-ticket}

~~~python
async def create_support_ticket(case_id: UUID, ticket_url: str) -> SupportCase: ...
~~~

**Кто вызывает.** run_support_batch.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| case_id | UUID | Обращение в creating. | UUID(...) |
| ticket_url | str | Адрес из конфигурации RAG API + case_id. | https://bot.example/tickets/... |

### Что отдаёт

**Формат.** SupportCase.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | SupportCase | Открытый/уже созданный case с реальным ticket_url. | state="open" |

### Правила и ошибки

Одна транзакция creating→open с created_at, снимок исходного вопроса/ответа и ticket_link-доставка. Автоматический case привязывает терминальную доставку к вопросу; ручной не занимает новый слот. Уже open/closed возвращает сохранённый URL. При неподходящей стадии Conflict. История сохраняется до разрешения её очистки.

## <span className="operation-badge operation-badge--db">DB</span> `read_ticket_list` {#read-ticket-list}

~~~python
async def read_ticket_list(actor: Principal, limit: int) -> list[SupportCase]: ...
~~~

**Кто вызывает.** list_tickets.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Проверенный сотрудник/оператор. | role="employee" |
| limit | int, 1..100 | Предел выдачи. | 50 |

### Что отдаёт

**Формат.** Список SupportCase.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | list[SupportCase] | Только созданные open/closed, права применены до лимита. | [] |

### Правила и ошибки

Employee — только свои; operator/admin — все. Актуальный enabled/role проверяется по БД.

## <span className="operation-badge operation-badge--db">DB</span> `read_ticket` {#read-ticket}

~~~python
async def read_ticket(actor: Principal, case_id: UUID) -> TicketView: ...
~~~

**Кто вызывает.** get_ticket.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Проверенный пользователь. | Principal(...) |
| case_id | UUID | Тикет. | UUID(...) |

### Что отдаёт

**Формат.** TicketView.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | TicketView | Case, снимок контекста и реплики по порядку message_id. | TicketView(...) |

### Правила и ошибки

Только open/closed, employee видит своё. Несуществующий/чужой объект — NotFound, а не раскрытие его названия.

## <span className="operation-badge operation-badge--db">DB</span> `append_ticket_message` {#append-ticket-message}

~~~python
async def append_ticket_message(actor: Principal, case_id: UUID, client_message_id: UUID, text: str) -> TicketMessage: ...
~~~

**Кто вызывает.** add_ticket_message.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Автор. | Principal(...) |
| case_id | UUID | Открытый тикет. | UUID(...) |
| client_message_id | UUID | Ключ повтора реплики. | UUID(...) |
| text | непустой str | Содержимое, хранится как текст. | Не открывается |

### Что отдаёт

**Формат.** TicketMessage.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | TicketMessage | Сохранённая либо прежняя реплика. | message_id=9 |

### Правила и ошибки

Проверяет доступ и open. Уникальный ключ (case_id,author_id,client_message_id). Повтор с другим текстом — Conflict. Автор/роль берутся из проверенного доступа.

## <span className="operation-badge operation-badge--db">DB</span> `close_ticket_record` {#close-ticket-record}

~~~python
async def close_ticket_record(actor: Principal, case_id: UUID) -> SupportCase: ...
~~~

**Кто вызывает.** close_ticket.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Operator/admin. | role="operator" |
| case_id | UUID | Тикет. | UUID(...) |

### Что отдаёт

**Формат.** SupportCase.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | SupportCase | Закрытое обращение. | state="closed" |

### Правила и ошибки

Первое закрытие сохраняет closed_at; повтор не меняет это время и безопасен. Employee не закрывает тикет этим методом.

## <span className="operation-badge operation-badge--db">DB</span> `read_metrics` {#read-metrics}

~~~python
async def read_metrics(actor: Principal, from_at: datetime, to_at: datetime) -> MetricsSnapshot: ...
~~~

**Кто вызывает.** get_metrics.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Admin. | role="admin" |
| from_at | UTC datetime | Начало включительно. | UTC |
| to_at | UTC datetime | Конец исключительно. | UTC |

### Что отдаёт

**Формат.** MetricsSnapshot.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | MetricsSnapshot | Уникальные вопросы, ответы, кейсы, реакции и задержки. | requests=10 |

### Правила и ошибки

Запросы относятся к received_at периода; answered/escalated — их зафиксированные итоги, dissatisfied — вопросы с зарегистрированным offer(reason=dissatisfied). Повторы/попытки не увеличивают счётчик. reply_duration_ms использует первое confirmed_at нормальной текстовой части answer и received_at; ticket_duration_ms берёт closed_at минус received_at вопроса, только для закрытых кейсов; fallback не считается доставкой исходного ответа.

Автоматическое создание тикета по SupportOutcome принятого вопроса не требует повторного разрешения сотрудника на новый вопрос. Отдельные новые действия пользователя и web-просмотр по-прежнему проходят собственную проверку текущего допуска/роли.
