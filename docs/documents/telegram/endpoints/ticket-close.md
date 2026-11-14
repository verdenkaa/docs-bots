---
title: "POST /tickets/{case_id}/close"
sidebar_position: 9
toc_max_heading_level: 2
hide_title: true
---

:::warning Открыт механизм web-входа
Бизнес-правила и целевые DTO описаны. Способ аутентификации web-интерфейса пока не выбран; до подключения доверенного адаптера защищённые ручки возвращают auth_not_configured. DTO и точки входа каркаса синхронизированы; прикладная реализация и web-адаптеры ещё впереди.
:::

# <span className="operation-badge operation-badge--post">POST</span> `/tickets/{case_id}/close`

## Назначение и доступ

Закрывает operator/admin. Личность берётся только из доверенного web-входа в Principal. Способ входа пока открыт; пока адаптер не подключён, защищённая ручка возвращает 503 auth_not_configured, а не доверяет user_id/role из тела.

## Что принимает

case_id: UUID в пути, тела нет.

## Бизнес-логика

Вызвать close_ticket(actor,case_id). Повторное закрытие возвращает прежний закрытый тикет, история не удаляется.

## Что отдаёт

200 OK, application/json, SupportCase со state=closed.

## Ошибки и повторы

| HTTP | Причина | Действие |
| --- | --- | --- |
| 401 | Нет проверенной личности. | Войти. |
| 403 | Недостаточно роли. | Нужен operator/admin. |
| 404 | Нет тикета. | Проверить адрес. |
| 422 | Неверный UUID. | Исправить. |
| 503 | Вход/БД недоступны. | Безопасно повторить тот же запрос. |

## Связанные контракты

[close_ticket](../module/service-entrypoints.md#close-ticket)
