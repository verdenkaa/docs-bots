---
title: "POST /internal/telegram/updates"
sidebar_position: 2
toc_max_heading_level: 2
hide_title: true
---

# <span className="operation-badge operation-badge--post">POST</span> `/internal/telegram/updates`

## Назначение и доступ

Внутренний вход от единственного Telegram poller. Poller получает `getUpdates` и передаёт updates по Docker-сети; заголовок `X-Telegram-Intake-Secret` проверяется по `TELEGRAM_INTAKE_SECRET`. Внешний webhook не поддерживается. Публичный nginx закрывает `/internal/*`, прежний `/telegram/webhook` возвращает 404.

## Что принимает

Content-Type: application/json; тело — Telegram Update с неотрицательным int `update_id`. Личные сообщения содержат проверяемые `message.from.id`, `message.chat.id/type`, текст либо Telegram file_id изображения. Callback содержит `id`, `from`, `message.message_id`, личный чат и действие `a:/s:/y:/n:`. Дополнительные Telegram-поля допускаются, группы и неподдерживаемые updates пропускаются.

## Бизнес-логика

1. Проверить секрет, размер тела, JSON и update_id.
2. Передать в handle_telegram_update. `/start`, `/help` и обычные личные сообщения создают приглашение открыть Mini App через notice/Outbox. Аргументы и `@BotName` поддерживаются. Прочие slash-команды сохраняются как ignored без ответа. Навигация не требует допуска сотрудника и не раскрывает защищённые данные.
3. Старые callback обрабатывает прежний Support с проверкой допуска, владельца и кнопки. answerCallbackQuery принадлежит intake.
4. Telegram-вход не создаёт request или outbox rag.questions. При повторе возвращается прежний ledger, включая request_id исторического принятого вопроса. Новый RAG-вопрос приходит только через `/api/v1/questions`.

## Результат, ошибки и повторы

200 с пустым телом после надёжного решения; 400 при некорректном входе, 403 при неверном внутреннем секрете, 413 при превышении размера, 503 при неизвестном результате хранилища. После 200 poller увеличивает offset, при временной ошибке повторяет тот же update. Ledger и notice/outbox записываются атомарно; повтор не создаёт вторую доставку. Никаких sendMessage в HTTP-ответе: сообщения отправляет существующий results worker.

[Приём](../module/entrypoints.md#handle-telegram-update) · [HTTP-адаптер](../module/http-adapter-entrypoints.md) · [DTO](../module/contracts.md)
