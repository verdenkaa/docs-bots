"""Contracts: docs/documents/telegram/module/http-adapter-entrypoints.md."""
from fastapi import APIRouter, Response
from rag.contracts import StorageError
from rag.rag_api.config import get_settings
from rag.runtime.postgresql.rag_api import check_admission_ready

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("/live")
def health_live() -> Response:
    return Response(status_code=200)


@router.get("/ready")
async def health_ready() -> Response:
    try:
        get_settings()
        ready = await check_admission_ready()
    except (StorageError, RuntimeError, ValueError):
        ready = False
    return Response(status_code=200 if ready else 503)
