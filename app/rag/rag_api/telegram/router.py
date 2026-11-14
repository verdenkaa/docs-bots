import hmac
import json
import logging

from fastapi import APIRouter, Request, Response

from rag.rag_api.config import WEBHOOK_SECRET


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/telegram", tags=["Telegram"])


@router.post("/webhook")
async def telegram_webhook(request: Request) -> Response:
    # Проверка секретного токена
    secret = request.headers.get(
        "X-Telegram-Bot-Api-Secret-Token", ""
    )

    if not hmac.compare_digest(secret, WEBHOOK_SECRET):
        return Response(status_code=403)

    # Чтение и проверка сообщения
    try:
        update = await request.json()

        if not isinstance(update, dict):
            return Response(status_code=400)

        update_id = update["update_id"]

        if type(update_id) is not int:
            return Response(status_code=400)

        # Игнорируем остальные типы обновлений
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

        # Игнорируем сообщения без текста
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

    logger.info(
        "Telegram message | update_id=%s | chat_id=%s | text=%s",
        update_id,
        chat_id,
        text,
    )

    return Response(status_code=200)
