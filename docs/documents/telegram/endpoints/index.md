---
title: Эндпоинты RAG API
sidebar_position: 1
---

:::warning На согласовании
Входной режим выбираем по условиям кейса. Webhook открывает входную HTTP-ручку. Long polling получает обновления исходящими вызовами к Telegram.
:::

| Ручка | Кто вызывает | Назначение |
| --- | --- | --- |
| <span className="operation-badge operation-badge--post">POST</span> [`/telegram/webhook`](./telegram-webhook.md) | Telegram, только в режиме webhook | Доставить обновление и получить подтверждение приёма. |
| <span className="operation-badge operation-badge--get">GET</span> [`/health/live`](./health-live.md) | Среда запуска | Проверить, что HTTP-процесс отвечает. |
| <span className="operation-badge operation-badge--get">GET</span> [`/health/ready`](./health-ready.md) | Среда запуска | Проверить готовность регистрировать новые запросы. |

Webhook возвращает подтверждение. В long polling подтверждение происходит при продвижении `offset` в `getUpdates`. В обоих режимах Core обрабатывает вопрос через очередь, а RAG API отправляет ответ отдельным вызовом Telegram Bot API. [Приём и доставка через очередь](../module/queue-processing.md) · [Telegram Bot API](https://core.telegram.org/bots/api#getupdates)

Проверки `health` — предлагаемые внутренние ручки для обоих режимов, если среда запуска использует HTTP-проверки. [Сравнение режимов приёма](../module/index.md#два-способа-приёма)
