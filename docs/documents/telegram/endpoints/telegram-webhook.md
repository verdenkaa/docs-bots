---
title: "POST /telegram/webhook"
sidebar_position: 2
hide_title: true
---

:::warning На согласовании
Ручку включаем только при выборе webhook. Решение о режиме, список поддерживаемых чатов и команды бота уточним по кейсу.
:::

# <span className="operation-badge operation-badge--post">POST</span> `/telegram/webhook`

## Назначение и доступ

Telegram вызывает `POST /telegram/webhook` по HTTPS для доставки обновлений. RAG API проверяет `X-Telegram-Bot-Api-Secret-Token` по секрету, заданному при регистрации webhook. [Telegram Bot API](https://core.telegram.org/bots/api#setwebhook)

## Что принимает

`Content-Type: application/json`. Параметры пути и строки запроса отсутствуют. Тело — объект Telegram Update.

| Поле | Тип и обязательность | Для чего |
| --- | --- | --- |
| `X-Telegram-Bot-Api-Secret-Token` | Обязательный строковый заголовок. | Проверка источника обновления. |
| `update_id` | Обязательное целое число. | Дедупликация в пределах бота. |
| `message.chat.id` | Целое число для текстового сообщения. | Назначение ответа и порядок внутри чата. |
| `message.text` | Строка для текстового вопроса. | Исходный вопрос для RAG Core. Пригодность текста проверяет Guardrails. |

Пример входящего запроса. Остальные поля Telegram могут присутствовать. [Telegram Update](https://core.telegram.org/bots/api#update)

```http
POST /telegram/webhook HTTP/1.1
Host: bot.example.org
Content-Type: application/json
X-Telegram-Bot-Api-Secret-Token: example_secret

{
  "update_id": 10001,
  "message": {
    "message_id": 7,
    "date": 1760000000,
    "chat": { "id": 123456789, "type": "private" },
    "text": "Где найти инструкцию по установке?"
  }
}
```

## Бизнес-логика

1. Проверяем секрет и валидность обновления. Корректное обновление вне поддерживаемого сценария подтверждаем без постановки вопроса.
2. Регистрируем вопрос и запись outbox для `rag.questions` одной транзакцией. Повторный `update_id` уже принятого обновления возвращает подтверждение и использует существующий запрос.
3. Отвечаем Telegram после коммита. Queue Relay публикует уведомление отдельно, а готовый результат RAG API отправляет в чат. [Приём и доставка через очередь](../module/queue-processing.md)

## Что отдаёт

`200 OK` с пустым телом. Такой ответ подтверждает принятую, повторную или корректную пропущенную доставку Telegram Update.

## Ошибки и повторы

| HTTP-код | Код ошибки, если предусмотрен | Когда возникает | Действие клиента |
| --- | --- | --- | --- |
| `400` | — | Некорректный JSON или обязательные данные обновления отсутствуют. | Проверить формат отправляемого Update. |
| `403` | — | Секрет отсутствует или не совпал. | Проверить настройку секрета webhook. |
| `503` | — | PostgreSQL не позволяет подтвердить транзакцию или достигнут лимит незавершённых запросов при приёме нового вопроса. | Telegram может повторить доставку обновления. |

Если Telegram повторит запрос после таймаута, дедупликация по `update_id` защитит от второй постановки вопроса. Повторные доставки Telegram ограничены. [Telegram Bot API](https://core.telegram.org/bots/api#setwebhook)

## Связанные контракты

[Модуль RAG API](../module/index.md) · [Маршруты RabbitMQ](../../architecture/message-flow.md)
