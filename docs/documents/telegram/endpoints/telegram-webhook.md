---
title: "POST /telegram/webhook"
sidebar_position: 2
toc_max_heading_level: 2
hide_title: true
---

# <span className="operation-badge operation-badge--post">POST</span> `/telegram/webhook`

## Назначение и доступ

Telegram доставляет update по HTTPS. Проверяется X-Telegram-Bot-Api-Secret-Token; это подлинность Telegram, а допуск сотрудника отдельно проверяет Access.

## Что принимает

Content-Type: application/json, обязательный секрет в заголовке. Тело — Telegram Update.

| Поле | Тип / обязательность | Смысл / пример |
| --- | --- | --- |
| update_id | int, обязательно | 10001, дедупликация. |
| message.from.id | int для сообщения | ID сотрудника. |
| message.chat.id / type | int / строка | Личный чат, type=private. |
| message.text | str для текста | Самостоятельный вопрос либо явная команда. |
| message.photo / caption | список PhotoSize / str или отсутствует | Изображение и подпись; выбирается file_id. |
| message.document | объект, только разрешённое изображение | file_id и проверяемый формат; обычные документы идут в Ingestion, не в RAG-вопрос. |
| callback_query.id / from.id | str / int для callback | Квитанция нажатия и отправитель. |
| callback_query.message.chat / data | объект / str для callback | Только личный чат; действие a:/s:/y:/n:. |

Поля Telegram вне используемого сценария могут присутствовать; неподдерживаемый корректный update — ignored.

## Бизнес-логика

1. Проверить секрет, JSON и структуру update.
2. Передать в handle_telegram_update. Он проверяет личный чат, допуск, повтор, вопрос либо управление.
3. accepted/duplicate/handled/busy/denied/ignored подтверждаются после сохранённого решения. Retry_later требует повторной доставки; Core работает вне HTTP-запроса.
4. Для callback транспорт отдельно вызывает answerCallbackQuery; это не HTTP-тело ответа webhook.

## Что отдаёт

200 OK с пустым телом для надёжно обработанного update. Бизнес-отказ доступа/занятости сотрудника не является HTTP-403/503 и не ставит вопрос повторно.

## Ошибки и повторы

| HTTP | Причина | Действие |
| --- | --- | --- |
| 400 | Некорректный JSON или нет обязательного update_id/структуры. | Исправить вход. |
| 403 | Отсутствует/неверен секрет Telegram. | Проверить настройку webhook. |
| 503 | retry_later: PostgreSQL/коммит неизвестны либо глобальная ёмкость исчерпана. | Telegram может повторить тот же update. |

## Связанные контракты

[Приём](../module/entrypoints.md#handle-telegram-update) · [DTO](../module/contracts.md) · [Telegram Bot API](https://core.telegram.org/bots/api#setwebhook)
