---
title: "POST /tickets/{case_id}/close"
sidebar_position: 9
toc_max_heading_level: 2
hide_title: true
---

# <span className="operation-badge operation-badge--post">POST</span> `/tickets/{case_id}/close`

## Назначение и доступ

Закрытие из админ-панели, посетитель по ссылке не может закрыть тикет. HTTP Basic по HTTPS: статичные ADMIN_LOGIN и ADMIN_PASSWORD из окружения/.env. HTTP-адаптер проверяет credentials и создаёт Principal(login=ADMIN_LOGIN); тело запроса не задаёт личность. Пустая конфигурация — 503 auth_not_configured, нет/неверны credentials — 401 с WWW-Authenticate. Для мутаций проверяется Origin панели. См. [правила входа](../module/access.md#администрирование).

## Что принимает

case_id: UUID; тела нет.

## Бизнес-логика

Вызвать close_ticket(actor,case_id). Первое закрытие сохраняет closed_at; повтор возвращает прежний результат без изменения времени.

## Что отдаёт

200 OK, application/json, SupportCase со state=closed.

## Ошибки и повторы

422 — неверный UUID/тело; 404 — тикет отсутствует или ещё не создан; 503 — БД/коммит недоступны. 401 — вход не подтверждён; 403 — неверный Origin; 503 — также отсутствует конфигурация входа. Повторное закрытие безопасно.

## Связанные контракты

[close_ticket](../module/service-entrypoints.md#close-ticket)
