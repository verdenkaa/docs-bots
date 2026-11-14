---
title: "Точки входа доступа и поддержки"
sidebar_position: 6
toc_max_heading_level: 2
---

Типы и поля: [DTO](./contracts.md). Техническая ошибка БД — StorageError. Отсутствующий/ещё не созданный тикет — NotFound; отсутствие административных полномочий — AccessDenied; неприменимое действие — Conflict. Web-адаптер отображает их в HTTP; Telegram получает безопасное пояснение.

## <span className="operation-badge operation-badge--async">ASYNC</span> `check_access` {#check-access}

~~~python
async def check_access(username: str | None) -> AccessRecord | None:
    ...
~~~

**Кто вызывает.** Приём RAG API; использует Runtime.read_user_access.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| username | str или None | Нормализованный Telegram from.username; None не допускается. | employee_name |

### Что отдаёт

**Формат.** AccessRecord или None.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | AccessRecord | Допуск текущего username. | enabled=True |
| return | None | Неизвестный/выключенный пользователь. | None |

### Правила и ошибки

Сбой БД не даёт разрешение. register_question и record_support_action атомарно проверяют допуск повторно при записи.

## <span className="operation-badge operation-badge--async">ASYNC</span> `set_user_access` {#set-user-access}

~~~python
async def set_user_access(actor: Principal, record: AccessRecord) -> AccessRecord: ...
~~~

**Кто вызывает.** Админ-панель после аутентификации.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Проверенный администратор. | login="admin" |
| record | AccessRecord | Канонический username и enabled. | username="employee_name", enabled=True |

### Что отдаёт

**Формат.** AccessRecord.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | AccessRecord | Сохранённое состояние сотрудника. | login="admin" |

### Правила и ошибки

Principal создаётся HTTP-адаптером только после проверки ADMIN_LOGIN/ADMIN_PASSWORD. Вызывает Runtime.write_user_access; допуск бота хранится по username, роли Telegram-пользователя отсутствуют. enabled=False запрещает новые вопросы; уже принятый request, его доставка и автоматическая эскалация завершаются. Изменение допуска не посылает отмену Core.

## <span className="operation-badge operation-badge--async">ASYNC</span> `handle_support_action` {#handle-support-action}

~~~python
async def handle_support_action(command: SupportCommand) -> IntakeResult: ...
~~~

**Кто вызывает.** handle_telegram_update для реакции/просьбы/кнопки.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| command | SupportCommand | Пользователь, update и нормализованное действие. | action.kind="start" |

### Что отдаёт

**Формат.** IntakeResult.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| kind / request_id / case_id / delivery_id | исход и ID или None | Повтор либо сохранённое управление/уведомление. | "handled" / 1042 / UUID(...) / 2087 |

### Правила и ошибки

Для callback передаёт origin и callback_message_id без потери; для текстовой просьбы origin=text, action=offer, callback_message_id=None. Для start вычисляет check_at=received_at+10 минут, иначе None; вызывает Runtime.record_support_action. Offer без исходного успешного доставленного ответа лишь просит сначала задать вопрос. Чужой/устаревший callback не меняет case, но update подтверждается. Подтверждение не создаёт новый RAG-вопрос.

## <span className="operation-badge operation-badge--task">TASK</span> `run_support_batch` {#run-support-batch}

~~~python
async def run_support_batch(limit: int) -> int: ...
~~~

**Кто вызывает.** run_api_maintenance.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| limit | int > 0 | Предел обработки готовых обращений. | 20 |

### Что отдаёт

**Формат.** Целое число.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | int >= 0 | Количество сохранённых переходов. | 2 |

### Правила и ошибки

list_pending_support(now,limit): waiting с наступившим сроком → queue_support_confirmation; creating → create_support_ticket(case_id,ticket_url). URL собирается из конфигурации и case_id. Создание собственной тикет-системы — атомарный переход PostgreSQL, не внешний HTTP-вызов. Повтор видит прежний ticket_url; уведомления идут через rag.results.

## <span className="operation-badge operation-badge--async">ASYNC</span> `list_tickets` {#list-tickets}

~~~python
async def list_tickets(actor: Principal, limit: int = 50) -> list[SupportCase]: ...
~~~

**Кто вызывает.** Web-страница списка.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Проверенный администратор. | login="admin" |
| limit | int, 1..100 | Предел выдачи. | 50 |

### Что отдаёт

**Формат.** Список SupportCase.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | list[SupportCase] | Только open/closed; список всех тикетов доступен только админ-панели. | [] |

### Правила и ошибки

HTTP-адаптер проверяет статичный вход до чтения списка. Waiting и cancelled не показываются как созданные тикеты.

## <span className="operation-badge operation-badge--async">ASYNC</span> `get_ticket` {#get-ticket}

~~~python
async def get_ticket(case_id: UUID) -> TicketView:
    ...
~~~

**Кто вызывает.** Web-страница тикета.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| case_id | UUID | Идентификатор из URL. | UUID(...) |

### Что отдаёт

**Формат.** TicketView.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| case / history / messages | DTO / списки | Обращение, снимок исходного вопроса и разговор. | TicketView(...) |

### Правила и ошибки

read_ticket читает созданный тикет по публичной ссылке, без проверки владельца. Минимальная история — один исходный вопрос с подготовленным текстом и реальным ответом, если он был; не весь диалог.

## <span className="operation-badge operation-badge--async">ASYNC</span> `add_ticket_message` {#add-ticket-message}

~~~python
async def add_ticket_message(actor: Principal | None, case_id: UUID, client_message_id: UUID, text: str) -> TicketMessage:
    ...
~~~

**Кто вызывает.** Web-форма разговора.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal или None | Проверенный оператор либо публичный посетитель. | None |
| case_id | UUID | Тикет из ссылки. | UUID(...) |
| client_message_id | UUID | Ключ повтора одного сообщения. | UUID(...) |
| text | str, непустой | Текст реплики, не исполняется как HTML. | "Не открывается" |

### Что отдаёт

**Формат.** TicketMessage.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | TicketMessage | Сохранённая реплика; повтор возвращает ту же. | message_id=9 |

### Правила и ошибки

Публичный маршрут передаёт actor=None (visitor); административный — проверенный Principal (operator и author_login). Уникальный ключ (case_id,author_role,client_message_id). Сначала проверяется повтор: совпадающий текст возвращает прежнюю реплику даже после закрытия, другой текст — Conflict. Новая реплика требует open. Неизвестный/ещё не созданный тикет — NotFound. Роль и логин не принимаются из тела.

## <span className="operation-badge operation-badge--async">ASYNC</span> `close_ticket` {#close-ticket}

~~~python
async def close_ticket(actor: Principal, case_id: UUID) -> SupportCase: ...
~~~

**Кто вызывает.** Оператор/администратор.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Проверенный администратор. | login="admin" |
| case_id | UUID | Закрываемый тикет. | UUID(...) |

### Что отдаёт

**Формат.** SupportCase.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | SupportCase | Закрытый тикет; повтор безопасен. | state="closed" |

### Правила и ошибки

Runtime.close_ticket_record требует Principal проверенного администратора. Закрытие не меняет историю и не означает автоматически, что ответ бота был успешен.

## <span className="operation-badge operation-badge--async">ASYNC</span> `get_metrics` {#get-metrics}

~~~python
async def get_metrics(actor: Principal, from_at: datetime, to_at: datetime) -> MetricsSnapshot: ...
~~~

**Кто вызывает.** Админ-панель.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| actor | Principal | Проверенный admin. | login="admin" |
| from_at | UTC datetime | Полуоткрытый период, from_at &lt; to_at. | 12:00 |
| to_at | UTC datetime | Полуоткрытый период, from_at &lt; to_at. | 13:00 |

### Что отдаёт

**Формат.** MetricsSnapshot.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | MetricsSnapshot | Наблюдаемые счётчики и задержки. | requests=10 |

### Правила и ошибки

Runtime.read_metrics считает уникальные вопросы/кейсы/реакции и подтверждённые доставки, не попытки. Процент решённых проблем не выдаём без утверждённой формулы. Тема пока не размечена надёжно: разрез по темам остаётся открытым, не заполняется выдуманными значениями.
