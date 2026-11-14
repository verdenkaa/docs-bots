---
title: "GET /admin/metrics"
sidebar_position: 11
toc_max_heading_level: 2
hide_title: true
---


# <span className="operation-badge operation-badge--get">GET</span> `/admin/metrics`

## Назначение и доступ

Администратор получает метрики. HTTP Basic по HTTPS: статичные ADMIN_LOGIN и ADMIN_PASSWORD из окружения/.env. HTTP-адаптер проверяет credentials и создаёт Principal(login=ADMIN_LOGIN); тело запроса не задаёт личность. Пустая конфигурация — 503 auth_not_configured, нет/неверны credentials — 401 с WWW-Authenticate. Для мутаций проверяется Origin панели. См. [правила входа](../module/access.md#администрирование).

## Что принимает

| Query | Тип / обязательность | Смысл |
| --- | --- | --- |
| from_at | datetime с часовым поясом, обязательно | Начало периода включительно. |
| to_at | datetime с часовым поясом, обязательно | Конец исключительно; to_at > from_at. |

GET без тела.

## Бизнес-логика

Вызвать get_metrics(actor,from_at,to_at). Счётчики относятся к уникальным вопросам периода, а не попыткам. Процент решённых проблем и темы не подставляются без согласованной методики.

## Что отдаёт

200 OK, application/json, MetricsSnapshot.

## Ошибки и повторы

| HTTP | Причина | Действие |
| --- | --- | --- |
| 401 | Нет/неверны credentials. | Войти. |
| 422 | Неверный период/время без зоны. | Исправить. |
| 503 | Вход не настроен / БД недоступна. | Повторить позже. |

## Связанные контракты

[get_metrics](../module/service-entrypoints.md#get-metrics) · [DTO метрик](../module/contracts.md#доступ-и-тикет)
