---
title: "POST /tickets/{case_id}/messages"
sidebar_position: 8
toc_max_heading_level: 2
hide_title: true
---

# <span className="operation-badge operation-badge--post">POST</span> `/tickets/{case_id}/messages`

## Назначение и доступ

Любой посетитель ссылки пишет в открытый тикет без входа. Этот маршрут всегда создаёт реплику «Посетитель», даже если браузер ранее вошёл в админ-панель.

## Что принимает

case_id: UUID. JSON: client_message_id (UUID, ключ повтора) и text (непустая строка, максимум 4096 символов). Поля автора/роли запрещены.

## Бизнес-логика

Вызвать add_ticket_message(None,case_id,client_message_id,text). author_role=visitor, author_login=None. Ключ (case_id,author_role,client_message_id). Совпадающий повтор возвращает прежнюю реплику, в том числе после закрытия; несовпадающий текст — Conflict. Только новая реплика требует open.

## Что отдаёт

200 OK, application/json, TicketMessage. Посетитель не выдаётся за владельца Telegram-вопроса.

## Ошибки и повторы

422 — неверный UUID/тело; 404 — тикет отсутствует или ещё не создан; 503 — БД/коммит недоступны. 409 — новый текст в закрытый тикет либо конфликт ключа. При неизвестном коммите повторять прежний client_message_id.

## Связанные контракты

[add_ticket_message](../module/service-entrypoints.md#add-ticket-message) · [Реплика оператора](./admin-ticket-message.md)


HTTP-тело ограничено 32768 байтами до JSON-разбора, с Content-Length и без него; переполнение — 413. Неизвестные поля запрещены.
Публичный тикет принимает максимум 20 новых реплик посетителей за скользящую минуту. Проверка атомарна в PostgreSQL и общая для HTTP-реплик. Превышение — 429 с Retry-After: 60. Совпадающий идемпотентный повтор не расходует квоту, включая повтор после закрытия. Операторские ответы доступны при исчерпанной публичной квоте.
