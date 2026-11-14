---
title: "GET /tickets/{case_id}"
sidebar_position: 7
toc_max_heading_level: 2
hide_title: true
---

# <span className="operation-badge operation-badge--get">GET</span> `/tickets/{case_id}`

## Назначение и доступ

Публичная ссылка: любой посетитель читает созданный тикет без входа, даже если он не автор Telegram-вопроса. Общего публичного списка нет.

## Что принимает

case_id: UUID в пути; без тела и query.

## Бизнес-логика

Вызвать get_ticket(case_id). Показать снимок одного вопроса/ответа и сообщения. Только open/closed; HTML экранируется. Форма посетителя отправляет на публичный маршрут messages.

## Что отдаёт

200 OK, text/html; charset=utf-8. Страница TicketView; закрытый тикет доступен только для чтения.

## Ошибки и повторы

422 — неверный UUID/тело; 404 — тикет отсутствует или ещё не создан; 503 — БД/коммит недоступны. Нет зависимости от ADMIN_LOGIN/ADMIN_PASSWORD и проверки employees.

## Связанные контракты

[get_ticket](../module/service-entrypoints.md#get-ticket) · [DTO](../module/contracts.md#доступ-и-тикет)
