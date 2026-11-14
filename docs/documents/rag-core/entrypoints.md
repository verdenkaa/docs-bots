---
title: "Точка входа RAG Core"
sidebar_position: 2
toc_max_heading_level: 2
---

## <span className="operation-badge operation-badge--sub">SUB</span> `handle_rag_question` {#handle-rag-question}

~~~python
async def handle_rag_question(event: RequestRef) -> ConsumeDecision: ...
~~~

**Кто вызывает.** Адаптер rag.questions.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| event.request_id | int | Ссылка на сохранённый вход. | 1042 |

### Что отдаёт

**Формат.** Одна строка ConsumeDecision.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | ack | Переход сохранён/уведомление не требует работы. | "ack" |
| return | retry_broker | Состояние либо коммит PostgreSQL неизвестны. | "retry_broker" |

### Правила и ошибки

claim_question возвращает input, prepared_text и поколение. Core ведёт [поток](./flow.md), сохраняет complete_question(attempt,outcome). Для ответа/пояснения транзакция создаёт отдельную доставку с delivery_id; для Support — устойчивый case/намерение. Сам обработчик не возвращает объект очереди и не вызывает Telegram. Relay публикует сохранённые outbox-уведомления; ACK относится только к входящему вопросу.
