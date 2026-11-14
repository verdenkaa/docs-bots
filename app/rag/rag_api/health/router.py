"""Contracts: docs/documents/telegram/module/http-adapter-entrypoints.md."""
import logging

from fastapi import APIRouter, Response


logger = logging.getLogger(__name__)

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("/live")
def health_live() -> Response:
    logger.info("Health check: Live")
    return Response(status_code=200)

@router.get("/ready")
def health_ready() -> Response:
    logger.info("Health check: Ready")
    return Response(status_code=200)
