"""Command redirection at the HTTP boundary; legacy Python intake is unchanged."""

from datetime import UTC, datetime
from fastapi.responses import JSONResponse
from rag.rag_api._mini_app_auth import _MiniSettings
from rag.runtime.postgresql.rag_api import record_update


async def _redirect(update):
    settings = _MiniSettings()
    if not settings.enabled or "callback_query" in update:
        return None
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender, chat = message.get("from"), message.get("chat")
    if (
        not isinstance(sender, dict)
        or not isinstance(chat, dict)
        or chat.get("type") != "private"
        or sender.get("is_bot") is not False
        or type(sender.get("id")) is not int
        or not 0 < sender["id"] < 2**63
        or type(chat.get("id")) is not int
        or not 0 < chat["id"] < 2**63
    ):
        return None
    url = settings._url()
    await record_update(
        update["update_id"], sender["id"], chat["id"], datetime.now(UTC), "ignored"
    )
    # Telegram supports answering a webhook with a Bot API method. This is only
    # navigation; no RAG question, slot or fabricated delivery receipt is created.
    return JSONResponse(
        {
            "method": "sendMessage",
            "chat_id": chat["id"],
            "text": "Чат с помощником и обращения в поддержку доступны в приложении.",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "Открыть приложение", "web_app": {"url": url}}]
                ]
            },
        }
    )
