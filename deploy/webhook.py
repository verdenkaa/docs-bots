"""Register only after public TLS and both application roles are healthy."""

import asyncio

import httpx

from rag.rag_api.config import get_settings
from rag.rag_api.telegram.webhook import register_webhook, telegram_api_request


async def register():
    settings = get_settings()
    async with httpx.AsyncClient(trust_env=False, timeout=15) as client:
        for attempt in range(12):
            try:
                # Validate the real public certificate and route before switching Telegram.
                response = await client.get(settings.public_url + "/health/live")
                response.raise_for_status()
                await register_webhook(client)
                result = await telegram_api_request(client, "getWebhookInfo")
                info = result.get("result", {})
                if info.get("url") != settings.public_url + "/telegram/webhook":
                    raise RuntimeError("Telegram did not confirm the configured webhook")
                print("Public HTTPS verified; Telegram webhook registered without dropping updates", flush=True)
                return
            except (httpx.HTTPError, RuntimeError):
                if attempt == 11:
                    raise RuntimeError("Webhook registration failed; check public HTTPS and Telegram connectivity") from None
                await asyncio.sleep(5)
