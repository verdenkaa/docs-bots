"""Contracts: docs/documents/telegram/module/http-adapter-entrypoints.md."""
import logging

import httpx

from rag.rag_api import config


logger = logging.getLogger(__name__)


async def telegram_api_request(client: httpx.AsyncClient, method: str, payload: dict | None = None,) -> dict:
    response = await client.post(f"{config.TELEGRAM_API_URL}/{method}", json=payload or {})

    response.raise_for_status()
    result = response.json()

    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(
            f"Telegram API {method} failed: {result}"
        )

    return result


async def register_webhook(client: httpx.AsyncClient,) -> None:
    webhook_url = config.WEBHOOK_BASE_URL + config.WEBHOOK_PATH

    await telegram_api_request(
        client,
        "setWebhook",
        {
            "url": webhook_url,
            "secret_token": config.WEBHOOK_SECRET,
            "allowed_updates": ["message"],
            "drop_pending_updates": False,
        },
    )

    logger.info("Webhook registered: %s", webhook_url)


async def unregister_webhook(client: httpx.AsyncClient, ) -> None:
    await telegram_api_request(client, "deleteWebhook")
    logger.info("Webhook deleted")