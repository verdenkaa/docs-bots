---
title: "GET /tickets"
sidebar_position: 6
toc_max_heading_level: 2
hide_title: true
---

# <span className="operation-badge operation-badge--get">GET</span> `/tickets`

## Назначение и доступ

Список всех созданных тикетов доступен только админ-панели. HTTP Basic по HTTPS: статичные ADMIN_LOGIN и ADMIN_PASSWORD из окружения/.env. HTTP-адаптер проверяет credentials и создаёт Principal(login=ADMIN_LOGIN); тело запроса не задаёт личность. Пустая конфигурация — 503 auth_not_configured, нет/неверны credentials — 401 с WWW-Authenticate. Для мутаций проверяется Origin панели. См. [правила входа](../module/access.md#администрирование).

## Что принимает

Query limit: int 1..100, по умолчанию 50.

## Бизнес-логика

Вызвать list_tickets(actor,limit). Выбрать open/closed по убыванию request_id до лимита; waiting/cancelled не выдавать как тикеты.

## Что отдаёт

200 OK, text/html; charset=utf-8. Список и навигация админ-панели.

## Ошибки и повторы

401 — неверные/отсутствующие credentials; 422 — неверный limit; 503 — не настроен вход или недоступна БД.

## Связанные контракты

[list_tickets](../module/service-entrypoints.md#list-tickets)
