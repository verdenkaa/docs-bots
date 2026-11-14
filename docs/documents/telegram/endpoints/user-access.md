---
title: "PUT /admin/users/{username}"
sidebar_position: 10
toc_max_heading_level: 2
hide_title: true
---

# <span className="operation-badge operation-badge--put">PUT</span> `/admin/users/{username}`

## Назначение и доступ

Администратор добавляет/отключает Telegram username в БД. HTTP Basic по HTTPS: статичные ADMIN_LOGIN и ADMIN_PASSWORD из окружения/.env. HTTP-адаптер проверяет credentials и создаёт Principal(login=ADMIN_LOGIN); тело запроса не задаёт личность. Пустая конфигурация — 503 auth_not_configured, нет/неверны credentials — 401 с WWW-Authenticate. Для мутаций проверяется Origin панели. См. [правила входа](../module/access.md#администрирование).

## Что принимает

username в пути: ASCII-буквы, цифры и подчёркивание, 1–32 символа после удаления начального @; приводится к нижнему регистру. JSON: enabled (bool, обязательно). Поля user_id/role не принимаются.

## Бизнес-логика

Собрать AccessRecord(username=нормализованное_имя,enabled=...). Вызвать set_user_access(actor,record). Повтор безопасен. Отключение сериализовано с новыми вопросами по username, но не отменяет уже принятые вопросы и доставки. Переименование — добавить новое имя и отключить старое.

## Что отдаёт

200 OK, application/json, сохранённый AccessRecord.

## Ошибки и повторы

401 — неверные credentials; 403 — неверный Origin; 422 — неверный username/тело; 503 — конфигурация входа/БД/коммит недоступны. Повторить то же состояние.

## Связанные контракты

[set_user_access](../module/service-entrypoints.md#set-user-access)
