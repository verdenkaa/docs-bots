---
title: "Эндпоинты RAG API"
sidebar_position: 1
toc_max_heading_level: 2
---


| Метод / путь | Кто вызывает | Назначение |
| --- | --- | --- |
| POST [/telegram/webhook](./telegram-webhook.md) | Telegram с секретом | Текст, изображение и callback только личного чата. |
| GET [/health/live](./health-live.md) | Среда запуска | HTTP-процесс жив. |
| GET [/health/ready](./health-ready.md) | Среда запуска | Готовность надёжного приёма и outbox. |
| GET [/sources/&#123;document_id&#125;](./source-page.md) | Браузер | Публичная HTML-заглушка только с названием документа. |
| GET [/tickets](./ticket-list.md) | Администратор | Список всех созданных тикетов. |
| GET [/tickets/&#123;case_id&#125;](./ticket-page.md) | Любой посетитель ссылки | Контекст и разговор в тикете. |
| POST [/tickets/&#123;case_id&#125;/messages](./ticket-message.md) | Любой посетитель ссылки | Реплика «Посетитель». |
| POST [/tickets/&#123;case_id&#125;/close](./ticket-close.md) | Администратор | Закрытие тикета. |
| PUT [/admin/users/&#123;username&#125;](./user-access.md) | Admin | Допуск Telegram username. |
| POST [/admin/tickets/&#123;case_id&#125;/messages](./admin-ticket-message.md) | Администратор | Реплика «Оператор». |
| GET [/admin/metrics](./metrics.md) | Admin | Наблюдаемые метрики за период. |

HTTP Basic по HTTPS: статичные ADMIN_LOGIN и ADMIN_PASSWORD из окружения/.env. HTTP-адаптер проверяет credentials и создаёт Principal(login=ADMIN_LOGIN); тело запроса не задаёт личность. Пустая конфигурация — 503 auth_not_configured, нет/неверны credentials — 401 с WWW-Authenticate. Для мутаций проверяется Origin панели. См. [правила входа](../module/access.md#администрирование). Публичные чтение и реплики тикета не зависят от входа администратора.

Webhook подтверждает приём, а результат приходит через очередь и отдельные вызовы Telegram. 20 секунд — метрика, не HTTP-таймаут ответа на вопрос. Long polling остаётся альтернативным адаптером, не вторым одновременным входом.

[Python-точки входа](../module/entrypoints.md) · [Access/Support](../module/service-entrypoints.md) · [DTO](../module/contracts.md)
