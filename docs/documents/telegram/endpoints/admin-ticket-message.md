---
title: "POST /admin/tickets/{case_id}/messages"
sidebar_position: 8.5
toc_max_heading_level: 2
hide_title: true
---

# <span className="operation-badge operation-badge--post">POST</span> `/admin/tickets/{case_id}/messages`

## Назначение и доступ

Реплика «Оператор» из админ-панели. HTTP Basic по HTTPS: статичные ADMIN_LOGIN и ADMIN_PASSWORD из окружения/.env. HTTP-адаптер проверяет credentials и создаёт Principal(login=ADMIN_LOGIN); тело запроса не задаёт личность. Пустая конфигурация — 503 auth_not_configured, нет/неверны credentials — 401 с WWW-Authenticate. Для мутаций проверяется Origin панели. См. [правила входа](../module/access.md#администрирование).

## Что принимает

case_id: UUID. JSON: client_message_id (UUID) и text (непустая строка). Автор/роль не принимаются.

## Бизнес-логика

После проверки входа вызвать add_ticket_message(actor,case_id,client_message_id,text). author_role=operator, author_login=actor.login. Порядок проверки повторов и состояния совпадает с публичной репликой, но пространство ключей отделено author_role.

## Что отдаёт

200 OK, application/json, TicketMessage.

## Ошибки и повторы

422 — неверный UUID/тело; 404 — тикет отсутствует или ещё не создан; 503 — БД/коммит недоступны. 401 — вход не подтверждён; 403 — Origin не соответствует панели; 409 — закрытый тикет для новой реплики/конфликт ключа; 503 — также отсутствуют env-credentials. Повторять прежний client_message_id.

## Связанные контракты

[add_ticket_message](../module/service-entrypoints.md#add-ticket-message) · [Публичная реплика](./ticket-message.md)
