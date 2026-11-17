"""Administrative HTTP upload adapter. See docs/documents/ingestion/http-upload.md."""

import asyncio
import secrets
from contextlib import asynccontextmanager
from uuid import UUID

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter, ValidationError
from starlette.requests import ClientDisconnect

from rag.contracts import StepError, StorageError
from rag.ingestion._gateway_settings import _GatewaySettings
from rag.ingestion.contracts import UploadInput, UploadRejected, UploadResult, UploadSource
from rag.ingestion.gateway import accept_upload
from rag.model_gateway.contracts import ModelBinding, ModelRole
from rag.model_gateway.entrypoints import close_models, start_models
from rag.model_gateway.providers import configure_coordination
from rag.runtime._config import RuntimeSettings
from rag.runtime._model_calls import _PostgresModelCalls
from rag.runtime.lifecycle import close_runtime, start_runtime
from rag.runtime._uploads import _sweep_orphans, _sweep_loop


@asynccontextmanager
async def _lifespan(app):
    settings = _GatewaySettings()
    if not settings.token.get_secret_value().strip():
        raise RuntimeError("RAG_INGESTION_TOKEN must be configured")
    app.state.ingestion_settings = settings
    coordinated = False
    cleanup_task = None
    stop = asyncio.Event()
    try:
        await start_runtime()
        runtime_settings = RuntimeSettings()
        bindings = TypeAdapter(dict[ModelRole, ModelBinding]).validate_json(
            runtime_settings.model_bindings.get_secret_value()
        )
        selected = {role: binding for role, binding in bindings.items() if role in {"ocr", "vision"}}
        if selected:
            configure_coordination(_PostgresModelCalls())
            coordinated = True
            async with asyncio.timeout(runtime_settings.startup_timeout_s):
                await start_models(selected)
        await _sweep_orphans(runtime_settings.batch_size)
        cleanup_task = asyncio.create_task(_sweep_loop(stop))
        yield
    finally:
        stop.set()
        if cleanup_task is not None:
            cleanup_task.cancel()
            await asyncio.gather(cleanup_task, return_exceptions=True)
        try:
            await close_models()
        finally:
            try:
                if coordinated:
                    configure_coordination(None)
            finally:
                app.state.ingestion_settings = None
                await close_runtime()


app = FastAPI(lifespan=_lifespan)


def _rejected(code, status):
    return JSONResponse(
        UploadRejected(kind="rejected", code=code).model_dump(mode="json"),
        status_code=status, headers={"Cache-Control": "no-store"},
    )


@app.post("/admin/ingestion/upload", status_code=202)
async def _upload(request: Request):
    settings = getattr(request.app.state, "ingestion_settings", None)
    if settings is None or not settings.token.get_secret_value().strip():
        raise HTTPException(503, "ingestion_unavailable", headers={"Cache-Control": "no-store"})
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not secrets.compare_digest(
        token.encode("utf-8"), settings.token.get_secret_value().encode("utf-8")
    ):
        raise HTTPException(401, "unauthorized", headers={
            "WWW-Authenticate": "Bearer", "Cache-Control": "no-store",
        })

    # Parse after authorization; no file body is consumed by FastAPI beforehand.
    try:
        source = UploadSource(
            filename=request.query_params.get("filename"),
            access_level=request.query_params.get("access_level"),
            source_url=request.query_params.get("source_url"),
        )
        value = request.query_params.get("document_id")
        document_id = UUID(value) if value is not None else None
    except (ValidationError, ValueError):
        raise HTTPException(422, "invalid_upload_metadata", headers={"Cache-Control": "no-store"}) from None
    mime = request.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    if mime not in {
        "application/octet-stream", "application/pdf",
        "text/markdown",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    } or not source.filename.lower().endswith((".pdf", ".docx", ".md", ".markdown")):
        return _rejected("unsupported_format", 415)
    length = request.headers.get("content-length")
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            return _rejected("invalid_upload", 400)
        # Avoid converting arbitrarily long client-controlled decimal strings.
        normalized = length.lstrip("0") or "0"
        limit = str(settings.max_upload_bytes)
        if len(normalized) > len(limit) or (len(normalized) == len(limit) and normalized > limit):
            return _rejected("too_large", 413)
        if int(normalized) == 0:
            return _rejected("invalid_upload", 400)
    try:
        result = TypeAdapter(UploadResult).validate_python(await accept_upload(
            UploadInput(content=request.stream(), source=source, document_id=document_id)
        ))
    except (StepError, StorageError, TimeoutError, NotImplementedError):
        raise HTTPException(503, "ingestion_unavailable", headers={"Cache-Control": "no-store"}) from None
    except ClientDisconnect:
        return _rejected("invalid_upload", 400)
    status = 202 if result.kind == "accepted" else {
        "unknown_document": 404, "invalid_upload": 400, "too_large": 413,
        "unsupported_format": 415, "conversion_failed": 422,
    }[result.code]
    return JSONResponse(result.model_dump(mode="json"), status_code=status,
                        headers={"Cache-Control": "no-store"})
