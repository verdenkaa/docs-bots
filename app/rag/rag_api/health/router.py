"""Contracts: docs/documents/telegram/module/http-adapter-entrypoints.md."""
from fastapi import APIRouter, Response

router = APIRouter(prefix="/health", tags=["Health"])


@router.get("/live")
def health_live() -> Response:
    return Response(status_code=200)


@router.get("/ready")
def health_ready() -> Response:
    # External blocker: Runtime has no public admission-readiness operation
    # checking PostgreSQL writes and global unfinished-request capacity.
    return Response(status_code=503)
