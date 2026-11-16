"""Authenticated HTTP Mini App channel. Legacy HTTP and Python entrypoints remain available."""

import re
from typing import Annotated
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Path, Query, Request, Response
from pydantic import ValidationError
from rag.rag_api._http import _json_body
from rag.rag_api._mini_app_auth import _identity, _admin_identity, _admin
from rag.rag_api.mini_app_contracts import (
    WebQuestionInput,
    WebSupportInput,
    WebAccessInput,
)
from rag.rag_api import support, access
from rag.rag_api.config import get_settings
from rag.rag_api.contracts import RateLimited, TicketMessageInput
from rag.runtime import s3
from rag.runtime.postgresql import _mini_app as storage

router = APIRouter(prefix="/api/v1", tags=["Mini App"])
_RequestId = Annotated[int, Path(ge=1, lt=2**63)]


async def _body(request, model):
    data = await _json_body(request)
    if (
        request.headers.get("content-type", "").split(";")[0].strip().lower()
        != "application/json"
    ):
        raise HTTPException(415, "json_required")
    try:
        return model.model_validate_json(data)
    except ValidationError:
        raise HTTPException(422, "invalid_body") from None


@router.get("/me")
async def _me(identity=Depends(_identity)):
    return await storage._me(identity)


@router.post("/questions", status_code=202)
async def _create_question(request: Request, identity=Depends(_identity)):
    key = request.headers.get("idempotency-key", "")
    if not re.fullmatch("[A-Za-z0-9._:-]{8,128}", key):
        raise HTTPException(422, "idempotency_key_required")
    body = await _body(request, WebQuestionInput)
    return await storage._register(identity, key, body)


@router.get("/questions/{request_id}")
async def _get_question(request_id: _RequestId, identity=Depends(_identity)):
    if not 0 < request_id < 2**63:
        raise HTTPException(422, "invalid_request_id")
    return await storage._question(identity, request_id)


@router.post("/uploads")
async def _uploads(identity=Depends(_identity)):
    # Explicitly disabled: no request body is read, no bytes are persisted, and
    # this route cannot enqueue a question or call OCR.
    raise HTTPException(501, "image_uploads_disabled")


@router.get("/images/{image_id}")
async def _image(image_id: UUID, identity=Depends(_identity)):
    await storage._image_allowed(identity, image_id)
    content = await s3.get_image(image_id)
    return Response(
        content.data,
        media_type=content.mime_type,
        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"},
    )


@router.post("/questions/{request_id}/support")
async def _start_support(
    request_id: _RequestId, request: Request, identity=Depends(_identity)
):
    body = await _body(request, WebSupportInput)
    case = await storage._start_support(identity, request_id, body.reason)
    if body.reason == "dissatisfied" and case.state == "creating":
        return await support.repository.create_support_ticket(
            case.case_id, get_settings().public_url + "/tickets/" + str(case.case_id)
        )
    return case


@router.get("/support/{case_id}")
async def _get_support(case_id: UUID, identity=Depends(_identity)):
    return await storage._support_case(identity, case_id)


@router.get("/support")
async def _pending_support(
    limit: int = Query(30, ge=1, le=100),
    before_request_id: int | None = Query(None, ge=1, lt=2**63),
    identity=Depends(_identity),
):
    return await storage._pending_support(identity, limit, before_request_id)


@router.post("/support/{case_id}/confirm")
async def _confirm(case_id: UUID, identity=Depends(_identity)):
    return await storage._support_case(identity, case_id, "confirm")


@router.post("/support/{case_id}/decline")
async def _decline(case_id: UUID, identity=Depends(_identity)):
    return await storage._support_case(identity, case_id, "decline")


@router.get("/tickets")
async def _tickets(
    limit: int = Query(30, ge=1, le=100),
    before_request_id: int | None = Query(None, ge=1, lt=2**63),
    identity=Depends(_identity),
):
    return await storage._tickets(identity, limit, before_request_id)


@router.get("/tickets/{case_id}/messages")
async def _messages(
    case_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    before_message_id: int | None = Query(None, ge=1, lt=2**63),
    identity=Depends(_identity),
):
    await storage._authorize_ticket(identity, case_id)
    return await support.get_ticket(case_id, limit, before_message_id)


@router.post("/tickets/{case_id}/messages")
async def _add_message(case_id: UUID, request: Request, identity=Depends(_identity)):
    body = await _body(request, TicketMessageInput)
    try:
        return await storage._append_message(identity, case_id, body)
    except RateLimited:
        raise HTTPException(
            429, "message_limit", headers={"Retry-After": "60"}
        ) from None


@router.get("/admin/tickets")
async def _admin_tickets(
    limit: int = Query(30, ge=1, le=100),
    before_request_id: int | None = Query(None, ge=1, lt=2**63),
    identity=Depends(_admin_identity),
):
    return await storage._tickets(identity, limit, before_request_id, admin=True)


@router.get("/admin/tickets/{case_id}/messages")
async def _admin_messages(
    case_id: UUID,
    limit: int = Query(50, ge=1, le=100),
    before_message_id: int | None = Query(None, ge=1, lt=2**63),
    identity=Depends(_admin_identity),
):
    await storage._authorize_ticket(identity, case_id, admin=True)
    return await support.get_ticket(case_id, limit, before_message_id)


@router.post("/admin/tickets/{case_id}/messages")
async def _admin_add_message(
    case_id: UUID, request: Request, identity=Depends(_admin_identity)
):
    _admin(identity)
    body = await _body(request, TicketMessageInput)
    try:
        return await storage._append_message(identity, case_id, body, admin=True)
    except RateLimited:
        raise HTTPException(
            429, "message_limit", headers={"Retry-After": "60"}
        ) from None


@router.post("/admin/tickets/{case_id}/close")
async def _close(case_id: UUID, identity=Depends(_admin_identity)):
    return await storage._close_ticket(identity, case_id)


@router.get("/admin/users")
async def _users(
    limit: int = Query(50, ge=1, le=100),
    after: str = Query("", max_length=32),
    identity=Depends(_admin_identity),
):
    return await storage._users(identity, limit, after)


@router.put("/admin/users/{username}")
async def _set_access(
    username: str, request: Request, identity=Depends(_admin_identity)
):
    _admin(identity)
    normalized = access._normalize_username(username)
    if normalized is None:
        raise HTTPException(422, "invalid_username")
    body = await _body(request, WebAccessInput)
    return await storage._set_access(identity, normalized, body.enabled)


@router.get("/admin/metrics")
async def _metrics(
    from_at: datetime, to_at: datetime, identity=Depends(_admin_identity)
):
    _admin(identity)
    if from_at.tzinfo is None or to_at.tzinfo is None or from_at >= to_at:
        raise HTTPException(422, "invalid_period")
    return await storage._metrics(identity, from_at, to_at)
