---
title: "Python-функции HTTP-адаптера"
sidebar_position: 20
---

:::warning
Приём и доставка подключены к Runtime. Для readiness пока нет публичной проверки
доступности PostgreSQL и свободной глобальной ёмкости; `/health/ready` возвращает 503.
:::

Публичные сигнатуры HTTP-адаптера сохранены. Прикладные сценарии описаны в
[точках входа](./entrypoints.md), внешние маршруты — в [эндпоинтах](../endpoints/index.md).

## <span className="operation-badge operation-badge--async">ASYNC</span> Жизненный цикл

~~~python
async def lifespan(app: FastAPI): ...
~~~

FastAPI передаёт приложение. Контекст проверяет настройки, запускает Runtime,
передаёт управление серверу через yield и закрывает Runtime при остановке или ошибке.
Временные обрывы подключения к PostgreSQL получают до трёх попыток в пределах
RAG_STARTUP_TIMEOUT_S. Ошибки аутентификации, конфигурации и схемы не повторяются.
HTTP workers не запускают consumers, модели и регистрацию webhook.

Импорт модулей не читает `.env` и не требует секретов. Настройки читаются при
использовании из окружения и необязательного `.env` текущего рабочего каталога;
запускать приложение следует из `app/`. Окружение имеет приоритет над файлом.
Используются TELEGRAM_BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET и TELEGRAM_WEBHOOK_URL
(публичный HTTPS base URL). BOT_TOKEN не используется. Секрет webhook — 1–256
ASCII-букв, цифр, подчёркиваний или дефисов. Секреты и локальный `.env` не коммитятся.

## <span className="operation-badge operation-badge--async">ASYNC</span> Telegram

~~~python
async def telegram_webhook(request: Request) -> Response: ...
async def telegram_api_request(client: httpx.AsyncClient, method: str, payload: dict | None = None,) -> dict: ...
async def register_webhook(client: httpx.AsyncClient,) -> None: ...
async def unregister_webhook(client: httpx.AsyncClient, ) -> None: ...
~~~

telegram_webhook проверяет секрет, Content-Type, JSON и update_id, затем передаёт
полный Update в handle_telegram_update. Поддерживаются личные сообщения с текстом,
изображениями и callback; группы и неподдерживаемые updates сохраняются как пропуск.
Несовпадение секрета даёт 403, некорректный ввод — 400, retry_later — 503,
надёжно обработанный Update — 200. Тело ответа пустое. Итоговое JSON-событие
содержит шаги и безопасные метаданные без вопросов, ответов и секретов.
Подтверждение callback принадлежит intake, а не роутеру.

telegram_api_request выполняет один Bot API-запрос переданным HTTPX-клиентом.
Неуспешный ответ или транспортная ошибка дают безопасный RuntimeError без
тела ответа и URL с токеном. register_webhook явно задаёт URL, секрет и
allowed_updates=[message, callback_query]; unregister_webhook явно удаляет
регистрацию. pending updates не удаляются. Эти операции вызываются оператором,
а не каждым HTTP worker; остановка HTTP не удаляет webhook работающих реплик.

## <span className="operation-badge operation-badge--call">CALL</span> Проверки процесса

~~~python
def health_live() -> Response: ...
def health_ready() -> Response: ...
~~~

health_live возвращает 200 с пустым телом без проверки внешних сервисов.
health_ready возвращает 503 с пустым телом до добавления публичного интерфейса
Runtime для достоверной проверки PostgreSQL и глобальной ёмкости приёма.
Доступность RabbitMQ сама по себе не должна запрещать приём через outbox.
