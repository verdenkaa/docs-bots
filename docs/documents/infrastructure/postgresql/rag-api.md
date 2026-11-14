---
title: "Контракты PostgreSQL · RAG API"
sidebar_position: 2
toc_max_heading_level: 2
---

Типы: [DTO RAG API](../../telegram/module/contracts.md), [общие DTO](../../rag-core/contracts.md). Время UTC. Повтор update проверяется **до** лимита/занятости и возвращает прежние ссылки. Бизнес-решение выбирает RAG API; здесь описаны согласованные проверки и запись состояния.

Доставки busy/denied/first_question не требуют content: текст формирует RAG API по purpose. В IntakeResult они возвращаются как delivery_id; уведомление и результат update сохраняются вместе.

## <span className="operation-badge operation-badge--db">DB</span> `register_question` {#register-question}

~~~python
async def register_question(question: NewQuestion) -> IntakeResult: ...
~~~

**Кто вызывает.** Приём RAG API.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| question | NewQuestion | Проверенный личный чат, отправитель, вход и время. | input.kind="image" |

### Что отдаёт

**Формат.** IntakeResult.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| kind / request_id / delivery_id | исход и ID или None | accepted, duplicate, busy, denied либо retry_later. | "accepted",1042,None |

### Правила и ошибки

Под блокировкой сотрудника проверяет ledger update, актуальный допуск и активный слот. accepted сохраняет question, ledger, слот и outbox rag.questions. busy/denied сохраняют только ledger и самостоятельное уведомление; нового вопроса нет. Глобальная перегрузка возвращает retry_later без фиксации окончательного исхода. У уже принятого update прежний исход не меняется, даже если сотрудника позднее отключили: ledger проверяется до нового решения о допуске. Новая регистрация и изменение enabled синхронизируются по одному user_id, поэтому решает порядок их коммитов.

## <span className="operation-badge operation-badge--db">DB</span> `record_update` {#record-update}

~~~python
async def record_update(update_id: int, user_id: int | None, chat_id: int | None, received_at: datetime, kind: Literal['ignored', 'denied', 'invalid_update']) -> IntakeResult: ...
~~~

**Кто вызывает.** Приём для неподдерживаемого типа/чата или отказа.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| update_id | int | Идентичность и время обработанного update. | 10001 |
| received_at | UTC datetime | Идентичность и время обработанного update. | UTC |
| user_id | int или None | Получатель, если достоверно известен личный чат. | 123 |
| chat_id | int или None | Получатель, если достоверно известен личный чат. | 123 |
| kind | перечисление | Выбранный RAG API окончательный исход. | "ignored" |

### Что отдаёт

**Формат.** IntakeResult.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | IntakeResult | Прежний исход либо сохранённый пропуск/отказ. | kind="ignored" |

### Правила и ошибки

Сохраняет ledger без вопроса. Для denied с известным личным получателем создаёт доставку purpose=denied. Неподдерживаемая группа не получает уведомление в чат. Повтор не создаёт второе уведомление.

## <span className="operation-badge operation-badge--db">DB</span> `claim_delivery` {#claim-delivery}

~~~python
async def claim_delivery(delivery_id: int) -> ClaimedDelivery | None: ...
~~~

**Кто вызывает.** handle_rag_result.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| delivery_id | int | Ссылка из DeliveryRef. | 2085 |

### Что отдаёт

**Формат.** ClaimedDelivery или None.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | ClaimedDelivery | Попытка, контент/case, план и прогресс. | next_part=1 |
| return | None | Отсутствует/завершено/занято/ещё рано; либо устаревший статус. | None |

### Правила и ошибки

Под блокировкой чата берёт первую готовую доставку по delivery_id, увеличивает попытку и фиксирует срок. Истёкшая sending-попытка отмечается неоднозначной. Загружает content и SupportCase по назначению, без обращения к Qdrant. Устаревший status/offer/confirmation отменяет; завершение/отмена будит следующую доставку через outbox. Доставки уже принятого request не отменяются из-за последующего enabled=False: текст, оставшиеся изображения и терминальная ссылка автоматической эскалации досылаются по сохранённому плану. Слот освобождается по обычному конечному исходу доставки.

## <span className="operation-badge operation-badge--db">DB</span> `store_delivery_parts` {#store-delivery-parts}

~~~python
async def store_delivery_parts(attempt: DeliveryAttempt, parts: Sequence[DeliveryPart]) -> bool: ...
~~~

**Кто вызывает.** handle_rag_result до первого Telegram-вызова.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| attempt | DeliveryAttempt | Взятая попытка. | delivery_id=2085 |
| parts | непустой список | Уже собранный неизменяемый план. | [TextPart(...),ImagePart(...)] |

### Что отдаёт

**Формат.** bool.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | bool | План сохранён/совпадает, либо попытка потеряла право. | True / False |

### Правила и ошибки

Проверяет срок, номер и sending. Первый план сохраняет; совпадающий повтор безопасен, другой план после начала отправки — ошибка контракта. Сетевой вызов запрещён до подтверждённого коммита.

## <span className="operation-badge operation-badge--db">DB</span> `record_delivery_part` {#record-delivery-part}

~~~python
async def record_delivery_part(attempt: DeliveryAttempt, part_no: int, message_id: int, variant: Literal['normal', 'length_fallback']) -> bool: ...
~~~

**Кто вызывает.** После Sent от send_answer.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| attempt | DeliveryAttempt | Право и индекс текущей части. | 2085 |
| part_no | int >= 0 | Право и индекс текущей части. | 0 |
| message_id | int | Реальная квитанция Telegram. | 77 |
| variant | вариант | Реальная квитанция Telegram. | "normal" |

### Что отдаёт

**Формат.** bool.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | bool | Квитанция сохранена и next_part сдвинут либо попытка устарела. | True |

### Правила и ошибки

Принимает только ожидаемую часть; совпадающий повтор не создаёт вторую квитанцию. Хранит Telegram message_id и confirmed_at текущей фиксации для callback и метрик. Повтор подтверждения сохраняет первое время, не перезаписывает его. При неизвестном коммите повтор читает прогресс; повторная отправка уже подтверждённой в БД части не выполняется.

## <span className="operation-badge operation-badge--db">DB</span> `schedule_delivery_retry` {#schedule-delivery-retry}

~~~python
async def schedule_delivery_retry(attempt: DeliveryAttempt, retry_at: datetime, uncertain: bool) -> bool: ...
~~~

**Кто вызывает.** RAG API после временного сбоя.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| attempt | DeliveryAttempt | Текущая попытка доставки. | 2085 |
| retry_at | UTC datetime | Следующий запуск и возможная отправка текущей части. | UTC |
| uncertain | bool | Следующий запуск и возможная отправка текущей части. | True |

### Что отдаёт

**Формат.** bool.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | bool | Ожидание и outbox rag.results сохранены либо попытка устарела. | True |

### Правила и ошибки

Сверяет срок/номер; сохраняет план, next_part, накопленную неоднозначность и available_at. Retry_after не продлевает технический предел повторов бесконечно.

## <span className="operation-badge operation-badge--db">DB</span> `complete_delivery` {#complete-delivery}

~~~python
async def complete_delivery(attempt: DeliveryAttempt, delivered: bool, uncertain: bool) -> bool: ...
~~~

**Кто вызывает.** После всех частей либо окончательной ошибки.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| attempt | DeliveryAttempt | Текущая попытка. | 2085 |
| delivered | bool | Все части подтверждены / хотя бы одна могла дойти. | True |
| uncertain | bool | Все части подтверждены / хотя бы одна могла дойти. | False |

### Что отдаёт

**Формат.** bool.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | bool | Конечное состояние сохранено либо попытка устарела. | True |

### Правила и ошибки

Сохраняет delivered, failed либо unknown с учётом прежней неоднозначности. Для delivered все части подтверждены. Только terminal_for_request освобождает активный слот исходного вопроса; status/offer/confirmation этого не делают. Под той же блокировкой будит следующую готовую доставку.

## <span className="operation-badge operation-badge--db">DB</span> `queue_waiting_statuses` {#queue-waiting-statuses}

~~~python
async def queue_waiting_statuses(now: datetime, limit: int) -> int: ...
~~~

**Кто вызывает.** run_api_maintenance.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| now | UTC datetime | Момент и предел пачки. | UTC |
| limit | int > 0 | Момент и предел пачки. | 20 |

### Что отдаёт

**Формат.** int.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | int >= 0 | Число новых статусов. | 2 |

### Правила и ошибки

Для вопроса без готового итога и с received_at &lt;= now-10 секунд создаёт один status и outbox. Уникальный ключ (request_id,purpose=status) защищает от повторов. 20 секунд здесь не проверяются как deadline.
