"""Contracts: docs/documents/telegram/module/http-adapter-entrypoints.md."""
import logging
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, Response

from rag.rag_api.telegram.router import router as telegram_router
from rag.rag_api.health.router import router as health_router
from rag.rag_api.telegram.webhook import (
    register_webhook,
    unregister_webhook,
)


logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    from rag.rag_api import config

    config._get_values()
    async with httpx.AsyncClient(timeout=10.0) as client:
        # Регистрируем webhook при запуске
        await register_webhook(client)

        try:
            yield
        finally:
            # Удаляем webhook при остановке
            try:
                await unregister_webhook(client)
            except Exception:
                logger.exception("Failed to delete Telegram webhook")


app = FastAPI(lifespan=lifespan)

# Подключение роутеров
app.include_router(telegram_router)
app.include_router(health_router)