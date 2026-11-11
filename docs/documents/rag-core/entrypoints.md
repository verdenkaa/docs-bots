---
title: "Точка входа RAG Core"
sidebar_position: 2
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

[Правила DTO и Pydantic](../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.



## <span className="operation-badge operation-badge--sub">SUB</span> `handle_rag_question` {#handle-rag-question}

~~~python
async def handle_rag_question(event: RequestRef) -> ConsumeDecision: ...
~~~

**Кто вызывает.** Адаптер RabbitMQ после проверки уведомления из `rag.questions`.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `event` | `RequestRef` | Ссылка на строку запроса в PostgreSQL. | `{"request_id": 1042}` |
| `event.request_id` | `int` | ID вопроса, передаваемый в claim_question. | `1042` |

### Что отдаёт

**Формат.** Одна строка Python, значение `ConsumeDecision`. Адаптер RabbitMQ применяет её к входящему сообщению из `rag.questions`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `Literal["ack"]` | Разрешает подтвердить доставку после сохранённого перехода или если работа уже не требуется. | `"ack"` |
| `return` | `Literal["retry_broker"]` | Просит повторную доставку с задержкой, когда состояние PostgreSQL не удалось проверить или сохранить. | `"retry_broker"` |

### Правила и ошибки

Обработчик загружает вопрос по `request_id` и ведёт его по [пайплайну RAG Core](./flow.md). Когда итог готов, [`complete_question(attempt, result)`](../infrastructure/postgresql/rag-core.md#complete-question) одной транзакцией сохраняет `RagResult` и создаёт запись outbox для `rag.results`. После коммита обработчик может вернуть `"ack"` для входящего сообщения.

[Queue Relay](../infrastructure/queue-relay.md) публикует в `rag.results` уведомление `{"request_id": 1042}`. RAG API по этому ID читает сохранённый ответ из PostgreSQL. [DTO уведомления](./contracts.md#очередь-и-готовый-результат)
