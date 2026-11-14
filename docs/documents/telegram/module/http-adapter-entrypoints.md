---
title: "Python-функции HTTP-адаптера"
sidebar_position: 20
---

:::warning
Текущий HTTP-адаптер регистрирует webhook и принимает текст для диагностики.
Надёжный приём, изображения и callback ещё требуется подключить к прикладным операциям.
:::

Сигнатуры существующих функций адаптера приведены ниже. Целевые HTTP-контракты —
в [эндпоинтах](../endpoints/index.md), бизнес-операции — в [точках входа](./entrypoints.md).

## <span className="operation-badge operation-badge--async">ASYNC</span> Жизненный цикл

~~~python
async def lifespan(app: FastAPI): ...
~~~

FastAPI передаёт приложение. Контекст проверяет настройки до открытия HTTP-клиента,
регистрирует webhook, передаёт управление серверу через yield, при остановке удаляет
webhook и закрывает клиент. Ошибка регистрации прерывает запуск; ошибка удаления логируется.

Импорт модулей не читает `.env` и не требует секретов. При запуске настройки берутся из
окружения; необязательный локальный `app/.env` дополняет только отсутствующие переменные.
Нужны BOT_TOKEN, TELEGRAM_WEBHOOK_SECRET и TELEGRAM_WEBHOOK_URL (HTTPS). Секрет webhook —
1–256 ASCII-букв, цифр, подчёркиваний или дефисов. Некорректная конфигурация прерывает
запуск до сетевых вызовов. Реальные секреты и локальный `.env` не включаются в репозиторий.

## <span className="operation-badge operation-badge--async">ASYNC</span> Telegram

~~~python
async def telegram_webhook(request: Request) -> Response: ...
async def telegram_api_request(client: httpx.AsyncClient, method: str, payload: dict | None = None,) -> dict: ...
async def register_webhook(client: httpx.AsyncClient,) -> None: ...
async def unregister_webhook(client: httpx.AsyncClient, ) -> None: ...
~~~

FastAPI вызывает telegram_webhook с Request. Несовпадение секрета даёт 403, ошибка
структуры JSON — 400; принятое либо игнорируемое обновление — 200. Текущий адаптер
диагностический и ещё не регистрирует вопрос в PostgreSQL.

Жизненный цикл передаёт открытый HTTPX-клиент остальным функциям. telegram_api_request
отправляет payload в заданный метод Telegram, возвращает JSON-объект; HTTP-ошибки
распространяются, неуспешный ответ API даёт RuntimeError. register_webhook задаёт URL,
секрет и allowed_updates; unregister_webhook удаляет webhook. Автоматических повторов нет.

## <span className="operation-badge operation-badge--call">CALL</span> Проверки процесса

~~~python
def health_live() -> Response: ...
def health_ready() -> Response: ...
~~~

FastAPI вызывает обе функции без параметров. Они возвращают HTTP 200; текущая
ready-проверка ещё не подтверждает готовность PostgreSQL и outbox.
