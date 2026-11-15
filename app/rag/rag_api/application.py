"""Contracts: docs/documents/telegram/module/http-adapter-entrypoints.md.

HTTP lifecycle and request logging; background roles belong to Runtime."""
import asyncio
from contextlib import asynccontextmanager

import asyncpg
from fastapi import FastAPI, Response
from sqlalchemy.exc import DBAPIError

from rag.rag_api.config import get_settings
from rag.rag_api._observability import _operation, _step, _utc_now
from rag.rag_api.health.router import router as health_router
from rag.rag_api.public import router as public_router
from rag.rag_api.telegram.router import router as telegram_router
from rag.rag_api.admin import router as admin_router
from rag.rag_api.contracts import AccessDenied, Conflict, NotFound
from rag.contracts import StorageError
from rag.runtime._config import RuntimeSettings
from rag.runtime.lifecycle import close_runtime, start_runtime


def _transient_startup_error(error):
    # SQLAlchemy may wrap the asyncpg exception. Never retry authentication,
    # configuration, missing-schema or arbitrary programming errors.
    visited = set()
    while error is not None and id(error) not in visited:
        visited.add(id(error))
        if isinstance(error, (ConnectionError, TimeoutError, asyncpg.ConnectionDoesNotExistError,
                              asyncpg.CannotConnectNowError, asyncpg.TooManyConnectionsError)):
            return True
        if isinstance(error, DBAPIError) and error.connection_invalidated:
            return True
        error = getattr(error, "orig", None) or error.__cause__
    return False


async def _start_with_retry():
    settings = RuntimeSettings()
    with _operation("startup", started=True) as trace:
        try:
            async with asyncio.timeout(settings.startup_timeout_s):
                for attempt_no in range(1, 4):
                    try:
                        with _step("start_runtime", error_code="runtime_start_failed", attempt_no=attempt_no):
                            await start_runtime()
                        trace.fields["outcome"] = "ready"
                        trace.fields.pop("error_code", None)
                        return
                    except Exception as error:
                        if not _transient_startup_error(error):
                            trace.fields.update(outcome="failed", error_code="runtime_start_failed")
                            raise
                        trace.fields.update(outcome="retry_pending", error_code="postgresql_connection_lost")
                        if attempt_no == 3:
                            break
                        # Runtime disposes partially created clients before raising.
                        with _step("startup_backoff", attempt_no=attempt_no):
                            await asyncio.sleep(2 ** (attempt_no - 1))
        except TimeoutError:
            trace.fields.update(outcome="failed", error_code="runtime_start_timeout")
            raise RuntimeError("Runtime startup timed out; check PostgreSQL availability and PGHOST/PGPORT") from None
        trace.fields.update(outcome="failed", error_code="postgresql_connection_lost")
        raise RuntimeError("PostgreSQL connection failed after 3 attempts; check the server, PGHOST/PGPORT and network") from None


@asynccontextmanager
async def _lifespan(app):
    get_settings()
    try:
        await _start_with_retry()
        yield
    finally:
        with _operation("shutdown") as trace:
            with _step("close_runtime", error_code="runtime_close_failed"):
                await close_runtime()
            trace.fields["outcome"] = "closed"


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Public HTTP lifecycle; preserve the documented adapter interface."""
    async with _lifespan(app):
        yield


app = FastAPI(lifespan=lifespan)


async def _business_error(request, error):
    code = 404 if isinstance(error, NotFound) else 409 if isinstance(error, Conflict) else 403 if isinstance(error, AccessDenied) else 503
    return Response(status_code=code, headers={'Cache-Control': 'no-store'})


for _error in (NotFound, Conflict, AccessDenied, StorageError):
    app.add_exception_handler(_error, _business_error)


@app.middleware("http")
async def _log_http(request, call_next):
    with _operation("http", method=request.method, received_at=_utc_now()) as trace:
        error = None
        try:
            response = await call_next(request)
        except Exception:
            error = "http_internal_error"
            response = Response(status_code=503)
        # BaseHTTPMiddleware executes the route in a child task; the shared trace
        # object collects its steps while ContextVar bindings remain task-local.
        trace.fields.update(route=getattr(request.scope.get("route"), "path", "unmatched"),
                            http_status=response.status_code)
        trace.fields.setdefault("outcome", "completed" if response.status_code < 500 else "unavailable")
        if error:
            trace.fields["error_code"] = error
        elif response.status_code >= 500:
            trace.fields.setdefault("error_code", "http_unavailable")
        response.headers.setdefault('Cache-Control', 'no-store')
        return response


app.include_router(telegram_router)
app.include_router(health_router)
app.include_router(public_router)
app.include_router(admin_router)
