---
title: "Контракты PostgreSQL · доступ и поддержка"
sidebar_position: 2.5
toc_max_heading_level: 2
---

[DTO](../../telegram/module/contracts.md). Все мутации идемпотентны и проверяют состояние внутри транзакции. Административный Principal поставляет HTTP-адаптер после проверки env-credentials; публичные тикеты не проверяют employees. StorageError — неизвестный коммит; AccessDenied, NotFound, Conflict — известный бизнес-отказ.

## <span className="operation-badge operation-badge--db">DB</span> `read_user_access` {#read-user-access}

~~~python
async def read_user_access(username: str | None) -> AccessRecord | None:
    ...
~~~

**Кто вызывает.** check_access.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| username | str или None | Канонический ключ списка допуска; None возвращает None. | employee_name |

### Что отдаёт

**Формат.** AccessRecord или None.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | AccessRecord или None | Запись допуска, включая enabled. | AccessRecord(...) |

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
| actor | Principal | Проверенный admin. | login="admin" |
| record | AccessRecord | Новое состояние сотрудника. | enabled=False |

### Что отдаёт

**Формат.** AccessRecord.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | AccessRecord | Сохранённая запись. | AccessRecord(...) |

### Правила и ошибки

Принимает Principal только от доверенного HTTP-адаптера после проверки статичных ADMIN_LOGIN/ADMIN_PASSWORD; employees не хранит права администратора. Одинаковое повторное изменение безопасно. Изменение допуска сериализуется с register_question по нормализованному username, включая создание отсутствующего ключа. enabled=False запрещает новые вопросы, но не отменяет ранее принятый request, его доставку или автоматическое создание тикета.

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

Одна транзакция: сначала ledger update, затем для нового действия проверка enabled по command.username, владельца по command.user_id и стадии и переход. offer требует AnsweredOutcome и подтверждённую текстовую часть (normal или length_fallback), не весь набор картинок; иначе создаёт first_question-уведомление. start находит сохранённую причину offer, создаёт один waiting-case и первый срок. confirm применим только к awaiting_confirmation и ставит creating; decline ставит cancelled. Повтор не сдвигает таймер. При origin=callback сверяются command.chat_id и callback_message_id с сохранённой квитанцией доставки, user_id с владельцем, action с кнопками сохранённого плана и request_id/case_id с этой доставкой. Одного известного ID вопроса недостаточно. Чужая/устаревшая команда не меняет case. Если квитанция ещё не сохранена, но соответствующая доставка находится в sending (гонка с отправкой), вернуть retry_later без окончательной записи ledger; повтор после фиксации квитанции сможет применить действие. Без подходящей квитанции и активной отправки действие считается устаревшим, ledger сохраняет ignored. Для origin=text допустим только offer: request_id при отсутствии выбирается как последний ответ этого user_id с подтверждённой текстовой частью; callback-сверка не выполняется.

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
| actor | Principal | Проверенный администратор. | login="admin" |
| limit | int, 1..100 | Предел выдачи. | 50 |

### Что отдаёт

**Формат.** Список SupportCase.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | list[SupportCase] | Только созданные open/closed, права применены до лимита. | [] |

### Правила и ошибки

Все open/closed для проверенного администратора; employees не участвует в web-доступе. Порядок по убыванию request_id до применения limit.

## <span className="operation-badge operation-badge--db">DB</span> `read_ticket` {#read-ticket}

~~~python
async def read_ticket(case_id: UUID, limit: int=50, before_message_id: int | None=None) -> TicketView:
    ...
~~~

**Кто вызывает.** get_ticket.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| case_id | UUID | Тикет. | UUID(...) |

### Что отдаёт

**Формат.** TicketView.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | TicketView | Case, снимок контекста и реплики по порядку message_id. | TicketView(...) |

### Правила и ошибки

Только open/closed, доступно любому посетителю ссылки. Отсутствующий или ещё не созданный тикет — NotFound. Проверки Telegram-владельца нет.

## <span className="operation-badge operation-badge--db">DB</span> `append_ticket_message` {#append-ticket-message}

~~~python
async def append_ticket_message(actor: Principal | None, case_id: UUID, client_message_id: UUID, text: str) -> TicketMessage:
    ...
~~~

**Кто вызывает.** add_ticket_message.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal или None | Проверенный оператор либо посетитель. | None |
| case_id | UUID | Открытый тикет. | UUID(...) |
| client_message_id | UUID | Ключ повтора реплики. | UUID(...) |
| text | непустой str | Содержимое, хранится как текст. | Не открывается |

### Что отдаёт

**Формат.** TicketMessage.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | TicketMessage | Сохранённая либо прежняя реплика. | message_id=9 |

### Правила и ошибки

actor=None означает visitor с author_login=None; Principal означает operator с author_login=actor.login. Уникальный ключ (case_id,author_role,client_message_id). Совпадающий повтор проверяется до состояния open и возвращает прежнюю реплику даже после закрытия; другой текст — Conflict. Новая реплика требует open. Посетители не имеют подтверждённого author_id. HTTP-адаптер не позволяет публичному маршруту передать Principal.

## <span className="operation-badge operation-badge--db">DB</span> `close_ticket_record` {#close-ticket-record}

~~~python
async def close_ticket_record(actor: Principal, case_id: UUID) -> SupportCase: ...
~~~

**Кто вызывает.** close_ticket.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Проверенный администратор. | login="admin" |
| case_id | UUID | Тикет. | UUID(...) |

### Что отдаёт

**Формат.** SupportCase.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | SupportCase | Закрытое обращение. | state="closed" |

### Правила и ошибки

Первое закрытие сохраняет closed_at; повтор не меняет это время и безопасен. Публичный посетитель не вызывает этот метод; нужен Principal администратора.

## <span className="operation-badge operation-badge--db">DB</span> `read_metrics` {#read-metrics}

~~~python
async def read_metrics(actor: Principal, from_at: datetime, to_at: datetime) -> MetricsSnapshot: ...
~~~

**Кто вызывает.** get_metrics.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Admin. | login="admin" |
| from_at | UTC datetime | Начало включительно. | UTC |
| to_at | UTC datetime | Конец исключительно. | UTC |

### Что отдаёт

**Формат.** MetricsSnapshot.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | MetricsSnapshot | Уникальные вопросы, ответы, кейсы, реакции и задержки. | requests=10 |

### Правила и ошибки

Запросы относятся к received_at периода; answered/escalated — их зафиксированные итоги, dissatisfied — вопросы с зарегистрированным offer(reason=dissatisfied). Повторы/попытки не увеличивают счётчик. reply_duration_ms использует первое confirmed_at нормальной текстовой части answer и received_at; ticket_duration_ms берёт closed_at минус received_at вопроса, только для закрытых кейсов; fallback не считается доставкой исходного ответа.

Автоматическое создание тикета по SupportOutcome принятого вопроса не требует повторного разрешения сотрудника на новый вопрос. Новые Telegram-действия проверяют текущий username; публичное чтение/реплики тикета не проверяют допуск Telegram, административные действия требуют входа по env.


### Страницы и ресурсные ограничения тикета

`read_ticket`/`get_ticket` принимают limit=50 (1..100) и before_message_id=None.
Без курсора выбирают последние реплики; внутри страницы порядок хронологический.
`TicketView.next_cursor` — ID старейшей реплики страницы, если есть более ранние.
SQL читает максимум limit+1 записей по индексу (case_id,message_id), без загрузки
всей истории. Новый ответ посетителя возвращает страницу к последним сообщениям.

Реплика ограничена 4096 символами. PostgreSQL под блокировкой тикета сначала
проверяет ключ идемпотентности, затем open и квоту 20 новых visitor-реплик за минуту.
`RateLimited` даёт HTTP 429; одинаковый повтор и ответы оператора квоту не расходуют.
На HTTP-границе действует отдельный предел тела 32768 байт до JSON-разбора.

### История и стоимость метрик

При создании тикета история копирует точный delivered_text из подтверждённых
квитанций, а не исходный ответ генератора. При отсутствии текстовой квитанции
answer_text=None. Квитанция fallback не считается нормальным текстовым ответом
в reply_duration_ms. Метрики агрегируются одним SQL-запросом; полные входы/ответы
и истории периода в Python не загружаются. Списки длительностей сохраняют контракт.
Последний доставленный вопрос для Support также выбирается одним ограниченным SQL-запросом.

### Mini App без изменения прежних контрактов

Для web-запроса снимок истории берёт сохранённый Markdown из QuestionOutcome:
это доступный по HTTP ответ, а не Telegram-квитанция. queue_support_confirmation
и create_support_ticket переводят те же стадии, но не создают Telegram-доставку
для web. Для telegram правила фактически доставленного текста сохраняются.
Прежний Principal по-прежнему означает Basic-администратора. Новый адаптер ролей
Telegram и новые операции хранения изолированы во внутренних файлах Mini App;
старые публичные ссылки/visitor-операции не получают новых ограничений.
[Контракт веб-канала](../../telegram/mini-app.md).
