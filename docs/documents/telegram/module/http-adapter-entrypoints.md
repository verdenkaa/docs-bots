---
title: "Python-функции HTTP-адаптера"
sidebar_position: 20
---

HTTP API Mini App сохраняет прежние пути и DTO. Telegram использует только внутренний polling intake. Прикладные сценарии описаны в
[точках входа](./entrypoints.md), внешние маршруты — в [эндпоинтах](../endpoints/index.md).

## <span className="operation-badge operation-badge--async">ASYNC</span> Жизненный цикл

~~~python
async def lifespan(app: FastAPI): ...
~~~

FastAPI передаёт приложение. Контекст проверяет настройки, запускает Runtime,
передаёт управление серверу через yield и закрывает Runtime при остановке или ошибке.
Временные обрывы подключения к PostgreSQL получают до трёх попыток в пределах
RAG_STARTUP_TIMEOUT_S. Ошибки аутентификации, конфигурации и схемы не повторяются.
HTTP workers не запускают consumers, модели или poller. Внешнего Telegram webhook и его регистрации нет.

Импорт модулей не читает `.env` и не требует секретов. Настройки читаются при
использовании из окружения и необязательного `.env` в корне репозитория (либо `RAG_ENV_FILE`);
запускать приложение следует из `app/`. Окружение имеет приоритет над файлом.
Общие настройки: `TELEGRAM_BOT_TOKEN` и `RAG_PUBLIC_URL` (публичный HTTPS base URL). Только внутренний приём и poller требуют `TELEGRAM_INTAKE_SECRET`: 1–256 ASCII-букв, цифр, подчёркиваний или дефисов. Mini App проверяет initData по токену и не зависит от intake-secret. Старые env-имена `TELEGRAM_WEBHOOK_URL` и `TELEGRAM_WEBHOOK_SECRET` читаются как совместимые алиасы, без включения webhook. Новые имена при одновременном задании в одном источнике имеют приоритет. Секреты не коммитятся.

## <span className="operation-badge operation-badge--async">ASYNC</span> Telegram

~~~python
async def telegram_updates(request: Request) -> Response: ...
~~~

Внутренняя ручка `POST /internal/telegram/updates` проверяет `X-Telegram-Intake-Secret`, Content-Type, JSON и update_id, затем передаёт Update в handle_telegram_update. Ручка вызывается poller по Docker-сети; публичный nginx возвращает 404 для `/internal/*` и удалённого `/telegram/webhook`.

Личные сообщения создают только приглашение открыть Mini App через существующий notice/Outbox. `/start`, `/help`, их аргументы и `@BotName` поддерживаются; остальные slash-команды молча сохраняются как ignored. Telegram-вход не создаёт RAG-вопрос. Старые callback проходят прежний Support, подтверждение callback принадлежит intake. Группы и неподдерживаемые updates сохраняются как пропуск.

Несовпадение секрета даёт 403, некорректный ввод — 400, retry_later — 503, надёжно обработанный Update — 200 с пустым телом. Ответы отправляет обычная доставка, Bot API-методы в HTTP-ответ не вкладываются. Итоговое JSON-событие содержит шаги и безопасные метаданные без вопросов, ответов и секретов.

## <span className="operation-badge operation-badge--async">ASYNC</span> Проверки процесса

~~~python
def health_live() -> Response: ...
async def health_ready() -> Response:
    ...
~~~

health_live возвращает 200 с пустым телом без проверки внешних сервисов.
health_ready вызывает check_admission_ready в Runtime и возвращает 200 при успешной
пробе записи и свободной ёмкости; 503 при ошибке настроек/БД или исчерпании лимита.
Доступность RabbitMQ сама по себе не должна запрещать приём через outbox.
