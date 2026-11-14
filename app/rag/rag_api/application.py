
import os
import hmac
import json
import logging

from pathlib import Path
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Request, Response


# ============================================================
# Логирование
# ============================================================

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


# ============================================================
# Загрузка .env без python-dotenv
# ============================================================

env_path = Path(__file__).resolve().parents[2] / ".env"

if not env_path.is_file():
    raise FileNotFoundError(
        f"Не найден файл .env: {env_path}"
    )

for line in env_path.read_text(encoding="utf-8-sig").splitlines():
    line = line.strip()

    if not line or line.startswith("#") or "=" not in line:
        continue

    key, value = line.split("=", 1)

    os.environ.setdefault(
        key.strip(),
        value.strip().strip('"').strip("'"),
    )


# ============================================================
# Конфигурация
# ============================================================

BOT_TOKEN = os.environ["BOT_TOKEN"]
WEBHOOK_SECRET = os.environ["TELEGRAM_WEBHOOK_SECRET"]
WEBHOOK_BASE_URL = os.environ["TELEGRAM_WEBHOOK_URL"].rstrip("/")
TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

WEBHOOK_PATH = "/telegram/webhook"
TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"


if not BOT_TOKEN:
    raise ValueError("BOT_TOKEN не задан")

if not WEBHOOK_SECRET:
    raise ValueError("TELEGRAM_WEBHOOK_SECRET не задан")

if not WEBHOOK_BASE_URL.startswith("https://"):
    raise ValueError(
        "TELEGRAM_WEBHOOK_URL должен начинаться с https://"
    )


# ============================================================
# Жизненный цикл приложения
# ============================================================

@asynccontextmanager
async def lifespan(app: FastAPI):
    webhook_url = WEBHOOK_BASE_URL + WEBHOOK_PATH

    async with httpx.AsyncClient(timeout=10.0) as client:
        # Регистрация webhook при запуске
        try:
            response = await client.post(
                f"{TELEGRAM_API_URL}/setWebhook",
                json={
                    "url": webhook_url,
                    "secret_token": WEBHOOK_SECRET,
                    "allowed_updates": ["message"],
                    "drop_pending_updates": False,
                },
            )

            response.raise_for_status()

            result = response.json()

            if not result.get("ok"):
                raise RuntimeError(
                    f"Telegram setWebhook failed: {result}"
                )

            logger.info(
                "Telegram webhook registered: %s",
                webhook_url,
            )

        except (httpx.HTTPError, ValueError, RuntimeError):
            logger.exception(
                "Failed to register Telegram webhook"
            )
            raise

        try:
            yield

        finally:
            # Удаление webhook при остановке
            try:
                response = await client.post(
                    f"{TELEGRAM_API_URL}/deleteWebhook"
                )

                response.raise_for_status()

                result = response.json()

                if not result.get("ok"):
                    raise RuntimeError(
                        f"Telegram deleteWebhook failed: {result}"
                    )

                logger.info("Telegram webhook deleted")

            except (httpx.HTTPError, ValueError, RuntimeError):
                logger.exception(
                    "Failed to delete Telegram webhook"
                )


# ============================================================
# FastAPI
# ============================================================

app = FastAPI(lifespan=lifespan)


@app.get("/health/live")
async def health_live():
    return Response(status_code=200)


@app.get("/health/ready")
async def health_ready():
    return Response(status_code=200)


# ============================================================
# Telegram Webhook
# ============================================================

@app.post(WEBHOOK_PATH)
async def telegram_webhook(request: Request):
    # Проверка секретного токена
    received_secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if not hmac.compare_digest(
        received_secret,
        WEBHOOK_SECRET,
    ):
        return Response(status_code=403)

    # Обработка JSON
    try:
        update = await request.json()

        if not isinstance(update, dict):
            return Response(status_code=400)

        update_id = update["update_id"]

        if type(update_id) is not int:
            return Response(status_code=400)

        # Пропускаем обновления без message
        if "message" not in update:
            return Response(status_code=200)

        message = update["message"]

        if not isinstance(message, dict):
            return Response(status_code=400)

        chat = message["chat"]

        if not isinstance(chat, dict):
            return Response(status_code=400)

        chat_id = chat["id"]

        if type(chat_id) is not int:
            return Response(status_code=400)

        # Пропускаем нетекстовые сообщения
        if "text" not in message:
            return Response(status_code=200)

        text = message["text"]

        if not isinstance(text, str):
            return Response(status_code=400)

    except (
        json.JSONDecodeError,
        KeyError,
        TypeError,
        ValueError,
    ):
        return Response(status_code=400)

    # Вывод в консоль
    logger.info(
        "Telegram message | update_id=%s | chat_id=%s | text=%s",
        update_id,
        chat_id,
        text,
    )

    return Response(status_code=200)
