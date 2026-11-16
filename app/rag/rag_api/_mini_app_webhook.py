"""Transport-independent Mini App navigation; legacy Python intake is unchanged."""

from datetime import UTC, datetime
from fastapi import Response
from rag.rag_api._mini_app_auth import _MiniSettings
from rag.rag_api.telegram import adapter
from rag.rag_api.delivery.limiter import limiter
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
    status, body = await adapter._post(
        "sendMessage",
        {
            "chat_id": chat["id"],
            "text": "Чат с помощником и обращения в поддержку доступны в приложении.",
            "reply_markup": {
                "inline_keyboard": [
                    [{"text": "Открыть приложение", "web_app": {"url": url}}]
                ]
            },
        },
        chat_id=chat["id"],
    )
    if status == 429 or body.get("error_code") == 429:
        parameters = body.get("parameters")
        delay = parameters.get("retry_after") if isinstance(parameters, dict) else None
        await limiter.defer(chat["id"], delay if type(delay) is int and delay >= 0 else 1)
        raise RuntimeError("mini_app_navigation_rate_limited")
    if status == 200 and body.get("ok") is True:
        result = body.get("result")
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if type(message_id) is not int or message_id <= 0:
            raise RuntimeError("mini_app_navigation_receipt_invalid")
    elif not (
        status in (400, 403) and body.get("ok") is False
        and body.get("error_code") == status
    ):
        raise RuntimeError("mini_app_navigation_failed")
    # A permanent per-chat rejection (e.g. the user blocked the bot) must not
    # stall polling for all users. Transient/unknown outcomes above remain
    # unacknowledged. Sending before recording also permits retries after a
    # failed send; an uncertain send or commit can show the button twice.
    await record_update(
        update["update_id"], sender["id"], chat["id"], datetime.now(UTC), "ignored"
    )
    return Response(status_code=200)
