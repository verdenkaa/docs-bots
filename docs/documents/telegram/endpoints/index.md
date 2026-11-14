---
title: "Эндпоинты RAG API"
sidebar_position: 1
toc_max_heading_level: 2
---

:::warning Открыт механизм web-входа
Бизнес-правила и целевые DTO описаны. Способ аутентификации web-интерфейса пока не выбран; до подключения доверенного адаптера защищённые ручки возвращают auth_not_configured. DTO и точки входа каркаса синхронизированы; прикладная реализация и web-адаптеры ещё впереди.
:::

| Метод / путь | Кто вызывает | Назначение |
| --- | --- | --- |
| POST [/telegram/webhook](./telegram-webhook.md) | Telegram с секретом | Текст, изображение и callback только личного чата. |
| GET [/health/live](./health-live.md) | Среда запуска | HTTP-процесс жив. |
| GET [/health/ready](./health-ready.md) | Среда запуска | Готовность надёжного приёма и outbox. |
| GET [/sources/&#123;document_id&#125;](./source-page.md) | Браузер | Публичная HTML-заглушка только с названием документа. |
| GET [/tickets](./ticket-list.md) | Сотрудник/оператор/admin | Web-список доступных тикетов. |
| GET [/tickets/&#123;case_id&#125;](./ticket-page.md) | Владелец/оператор/admin | Контекст и разговор в тикете. |
| POST [/tickets/&#123;case_id&#125;/messages](./ticket-message.md) | Владелец/оператор/admin | Идемпотентная реплика. |
| POST [/tickets/&#123;case_id&#125;/close](./ticket-close.md) | Оператор/admin | Закрытие тикета. |
| PUT [/admin/users/&#123;user_id&#125;](./user-access.md) | Admin | Допуск и роль интерфейса. |
| GET [/admin/metrics](./metrics.md) | Admin | Наблюдаемые метрики за период. |

Защищённые web-ручки не принимают Principal из JSON. При отсутствии адаптера входа — 503 auth_not_configured; при отсутствии проверенной личности — 401. Выбор cookie/token/Telegram login пока открыт. Если будущий вход использует cookie, адаптер обязан также задать защиту изменяющих запросов от CSRF.

Webhook подтверждает приём, а результат приходит через очередь и отдельные вызовы Telegram. 20 секунд — метрика, не HTTP-таймаут ответа на вопрос. Long polling остаётся альтернативным адаптером, не вторым одновременным входом.

[Python-точки входа](../module/entrypoints.md) · [Access/Support](../module/service-entrypoints.md) · [DTO](../module/contracts.md)
