"""Contracts: docs/documents/telegram/module/http-adapter-entrypoints.md.

Explicit operator operations; never called by HTTP worker lifespan."""
import httpx
from rag.rag_api.config import WEBHOOK_PATH, get_settings


async def _telegram_api_request(client, method: str, payload: dict | None = None) -> dict:
    try:
        response = await client.post(f"{get_settings().telegram_url}/{method}", json=payload or {})
        result = response.json()
    except (httpx.HTTPError, ValueError):
        raise RuntimeError("telegram_webhook_operation_failed") from None
    if response.status_code != 200 or not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("telegram_webhook_operation_failed")
    return result


async def _register_webhook(client) -> None:
    settings = get_settings()
    await _telegram_api_request(client, "setWebhook", {
        "url": settings.public_url + WEBHOOK_PATH,
        "secret_token": settings.webhook_secret,
        "allowed_updates": ["message", "callback_query"],
        "drop_pending_updates": False,
    })


async def _unregister_webhook(client) -> None:
    await _telegram_api_request(client, "deleteWebhook", {"drop_pending_updates": False})


async def telegram_api_request(client: httpx.AsyncClient, method: str, payload: dict | None = None) -> dict:
    """Call Bot API without exposing response bodies or token-bearing URLs."""
    return await _telegram_api_request(client, method, payload)


async def register_webhook(client: httpx.AsyncClient) -> None:
    """Explicit registration; not performed by the HTTP lifecycle."""
    await _register_webhook(client)


async def unregister_webhook(client: httpx.AsyncClient) -> None:
    """Explicit removal; never performed when an HTTP worker exits."""
    await _unregister_webhook(client)


if __name__ == "__main__":
    import argparse
    import asyncio

    parser = argparse.ArgumentParser(description="Explicit Telegram webhook management")
    parser.add_argument("operation", choices=("set", "delete"))
    args = parser.parse_args()

    async def _main():
        async with httpx.AsyncClient(timeout=get_settings().timeout_s, trust_env=False) as client:
            if args.operation == "set":
                await _register_webhook(client)
            else:
                await _unregister_webhook(client)

    asyncio.run(_main())
