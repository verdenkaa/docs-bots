---
title: "POST /tickets/{case_id}/messages"
sidebar_position: 8
toc_max_heading_level: 2
hide_title: true
---

:::warning Открыт механизм web-входа
Бизнес-правила и целевые DTO описаны. Способ аутентификации web-интерфейса пока не выбран; до подключения доверенного адаптера защищённые ручки возвращают auth_not_configured. DTO и точки входа каркаса синхронизированы; прикладная реализация и web-адаптеры ещё впереди.
:::

# <span className="operation-badge operation-badge--post">POST</span> `/tickets/{case_id}/messages`

## Назначение и доступ

Владелец и operator/admin добавляют реплику в открытый тикет. Личность берётся только из доверенного web-входа в Principal. Способ входа пока открыт; пока адаптер не подключён, защищённая ручка возвращает 503 auth_not_configured, а не доверяет user_id/role из тела.

## Что принимает

Content-Type: application/json. case_id: UUID в пути.

| Поле JSON | Тип / обязательность | Смысл |
| --- | --- | --- |
| client_message_id | UUID, обязательно | Один ключ на повтор отправки этой реплики. |
| text | непустой str, обязательно | Текст реплики, без исполнения HTML. |

Пример: `{"client_message_id":"e1246aa5-3bba-4a74-8445-60b6271c0551","text":"Ошибка остаётся"}`.

## Бизнес-логика

Передать actor и тело в add_ticket_message. Автор определяется по Principal. Уникальный ключ (case_id,author_id,client_message_id) предотвращает дубль.

## Что отдаёт

200 OK, application/json, TicketMessage; для повтора возвращается та же реплика с тем же message_id.

## Ошибки и повторы

| HTTP | Причина | Действие |
| --- | --- | --- |
| 401 | Нет проверенной личности. | Войти. |
| 404 | Чужой/неизвестный тикет. | Проверить адрес. |
| 409 | Тикет закрыт либо ключ повторён с другим текстом. | Не повторять автоматически с новым ключом. |
| 422 | Неверное тело. | Исправить. |
| 503 | Вход не настроен / неизвестный коммит. | Повторять с прежним client_message_id. |

## Связанные контракты

[add_ticket_message](../module/service-entrypoints.md#add-ticket-message) · [TicketMessage](../module/contracts.md#доступ-и-тикет)
