"""Internal web-channel transactions. Public legacy storage contracts are unchanged."""

import hashlib
from datetime import timedelta
from uuid import uuid4
from fastapi import HTTPException
from sqlalchemy import func, insert, select, update
from sqlalchemy import type_coerce
from sqlalchemy.dialects.postgresql import JSONB

from rag.rag_api.contracts import (
    AccessRecord,
    Conflict,
    NotFound,
    RateLimited,
    TicketMessage,
)
from rag.rag_api._mini_app_auth import _admin
from rag.runtime._state import _get
from ._db import _transaction, _lock, _one, _all, _execute, _now, _outbox
from ._models import _Employee, _Request, _WebRequest, _SupportCase, _SupportMessage
from ._support import _case, _ticket


async def _admitted(session, identity):
    row = await _one(
        session,
        select(_Employee.enabled).where(_Employee.username == identity.username),
    )
    return bool(row and row["enabled"])


async def _require_admission(session, identity):
    if not await _admitted(session, identity):
        raise HTTPException(403, "access_denied")


async def _me(identity):
    async with _transaction() as session:
        admitted = await _admitted(session, identity)
        active = await _one(
            session,
            select(_Request.request_id).where(
                _Request.user_id == identity.user_id,
                _Request.channel == "web",
                _Request.state != "finished",
            ),
        )
    return {
        **identity.model_dump(),
        "admitted": admitted,
        "active_request_id": active["request_id"] if active else None,
        "capabilities": {
            "text_questions": admitted,
            "image_uploads": False,
            "tickets": True,
            "admin": identity.role == "admin",
        },
    }


async def _register(identity, key, body):
    fingerprint = hashlib.sha256(body.model_dump_json().encode()).hexdigest()
    async with _transaction() as session:
        if identity.username is not None:
            await _lock(session, "username", identity.username)
        await _lock(session, "user", identity.user_id)
        old = await _one(
            session,
            select(_WebRequest).where(
                _WebRequest.user_id == identity.user_id,
                _WebRequest.idempotency_key == key,
            ),
        )
        if old:
            if old["payload_hash"] != fingerprint:
                raise Conflict("Idempotency key reused with different content")
            return {"request_id": old["request_id"]}
        await _require_admission(session, identity)
        if await _one(
            session,
            select(_Request.request_id).where(
                _Request.user_id == identity.user_id, _Request.state != "finished"
            ),
        ):
            raise HTTPException(409, "active_question_exists")
        await _lock(session, "admission", 0)
        count = (
            await _execute(
                session,
                select(func.count())
                .select_from(_Request)
                .where(_Request.state != "finished"),
            )
        ).scalar_one()
        if count >= _get().settings.max_pending_requests:
            raise HTTPException(503, "queue_full", headers={"Retry-After": "5"})
        row = await _one(
            session,
            insert(_Request)
            .values(
                channel="web",
                user_id=identity.user_id,
                username=identity.username,
                chat_id=None,
                update_id=None,
                input={"kind": "text", "text": body.text},
                received_at=await _now(session),
            )
            .returning(_Request.request_id),
        )
        request_id = row["request_id"]
        await _execute(
            session,
            insert(_WebRequest).values(
                request_id=request_id,
                user_id=identity.user_id,
                idempotency_key=key,
                payload_hash=fingerprint,
            ),
        )
        await _outbox(session, "rag.questions", request_id)
        return {"request_id": request_id}


async def _owned_request(session, identity, request_id):
    row = await _one(
        session,
        select(_Request).where(
            _Request.request_id == request_id,
            _Request.user_id == identity.user_id,
            _Request.channel == "web",
        ),
    )
    if row is None:
        raise NotFound("Question not found")
    return row


async def _question(identity, request_id):
    async with _transaction() as session:
        row = await _owned_request(session, identity, request_id)
        case = await _one(
            session, select(_SupportCase).where(_SupportCase.request_id == request_id)
        )
        return {
            "request_id": request_id,
            "state": row["state"],
            "text": row["input"]["text"],
            "received_at": row["received_at"],
            "outcome": row["result"],
            "support": _case(case) if case else None,
        }


async def _image_allowed(identity, image_id):
    async with _transaction() as session:
        # PostgreSQL JSONB containment verifies this image is in an answer owned
        # by this user; knowing an object UUID alone grants no access.
        found = await _one(
            session,
            select(_Request.request_id)
            .where(
                _Request.user_id == identity.user_id,
                _Request.channel == "web",
                type_coerce(_Request.result, JSONB)["result"]["images"].contains(
                    [{"image_id": str(image_id)}]
                ),
            )
            .limit(1),
        )
        if found is None:
            raise NotFound("Image not found")


async def _start_support(identity, request_id, reason):
    async with _transaction() as session:
        if identity.username is not None:
            await _lock(session, "username", identity.username)
        await _lock(session, "user", identity.user_id)
        request = await _owned_request(session, identity, request_id)
        existing = await _one(
            session, select(_SupportCase).where(_SupportCase.request_id == request_id)
        )
        if existing:
            if (reason == "dissatisfied" and existing["reason"] == "dissatisfied"
                    and existing["state"] in ("waiting", "awaiting_confirmation")):
                await _require_admission(session, identity)
                existing = await _one(
                    session,
                    update(_SupportCase)
                    .where(_SupportCase.case_id == existing["case_id"])
                    .values(state="creating", check_at=None)
                    .returning(*_SupportCase.__table__.c),
                )
            return _case(existing)
        await _require_admission(session, identity)
        if not request["result"] or request["result"]["kind"] != "answered":
            raise Conflict("Support action requires a completed answer")
        row = await _one(
            session,
            insert(_SupportCase)
            .values(
                case_id=uuid4(),
                request_id=request_id,
                user_id=identity.user_id,
                state="creating" if reason == "dissatisfied" else "waiting",
                reason=reason,
                check_at=None if reason == "dissatisfied" else await _now(session) + timedelta(minutes=10),
            )
            .returning(*_SupportCase.__table__.c),
        )
        return _case(row)


async def _owned_case(session, identity, case_id, admin=False, lock=False):
    if admin:
        _admin(identity)
    statement = select(_SupportCase).where(_SupportCase.case_id == case_id)
    if not admin:
        statement = statement.where(_SupportCase.user_id == identity.user_id)
    row = await _one(session, statement.with_for_update() if lock else statement)
    if row is None:
        raise NotFound("Support case not found")
    return row


async def _support_case(identity, case_id, action=None):
    async with _transaction() as session:
        await _lock(session, "user", identity.user_id)
        row = await _owned_case(session, identity, case_id, lock=True)
        request = await _one(
            session,
            select(_Request.channel).where(_Request.request_id == row["request_id"]),
        )
        if action and request["channel"] != "web":
            raise Conflict("Use the original Telegram callback for this support case")
        # The timer is persisted; reading may advance it even between maintenance batches.
        if (
            request["channel"] == "web"
            and row["state"] == "waiting"
            and row["check_at"] <= await _now(session)
        ):
            row = await _one(
                session,
                update(_SupportCase)
                .where(_SupportCase.case_id == case_id)
                .values(state="awaiting_confirmation")
                .returning(*_SupportCase.__table__.c),
            )
        if action:
            expected = "creating" if action == "confirm" else "cancelled"
            if row["state"] not in (expected, "open", "closed"):
                if row["state"] != "awaiting_confirmation":
                    raise Conflict("Support confirmation is not available")
                await _require_admission(session, identity)
                row = await _one(
                    session,
                    update(_SupportCase)
                    .where(_SupportCase.case_id == case_id)
                    .values(state=expected)
                    .returning(*_SupportCase.__table__.c),
                )
        return _case(row)


async def _pending_support(identity, limit, before):
    async with _transaction() as session:
        statement = (
            select(_SupportCase)
            .join(_Request, _Request.request_id == _SupportCase.request_id)
            .where(
                _SupportCase.user_id == identity.user_id,
                _Request.channel == "web",
                _SupportCase.state.in_(
                    ("waiting", "awaiting_confirmation", "creating")
                ),
            )
        )
        if before is not None:
            statement = statement.where(_SupportCase.request_id < before)
        rows = await _all(
            session, statement.order_by(_SupportCase.request_id.desc()).limit(limit + 1)
        )
        return {
            "items": [_case(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["request_id"] if len(rows) > limit else None,
        }


async def _tickets(identity, limit, before, admin=False):
    if admin:
        _admin(identity)
    async with _transaction() as session:
        statement = select(_SupportCase).where(
            _SupportCase.state.in_(("open", "closed"))
        )
        if not admin:
            statement = statement.where(_SupportCase.user_id == identity.user_id)
        if before is not None:
            statement = statement.where(_SupportCase.request_id < before)
        rows = await _all(
            session, statement.order_by(_SupportCase.request_id.desc()).limit(limit + 1)
        )
        return {
            "items": [_case(row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["request_id"] if len(rows) > limit else None,
        }


async def _authorize_ticket(identity, case_id, admin=False):
    async with _transaction() as session:
        await _owned_case(session, identity, case_id, admin=admin)
        await _ticket(session, case_id)


async def _append_message(identity, case_id, body, admin=False):
    async with _transaction() as session:
        row = await _owned_case(session, identity, case_id, admin=admin, lock=True)
        role, login = (
            ("operator", f"telegram:{identity.user_id}") if admin else ("visitor", None)
        )
        old = await _one(
            session,
            select(_SupportMessage).where(
                _SupportMessage.case_id == case_id,
                _SupportMessage.author_role == role,
                _SupportMessage.client_message_id == body.client_message_id,
            ),
        )
        if old:
            if old["text"] != body.text or old["author_login"] != login:
                raise Conflict("Message id reused with different content or author")
            return TicketMessage.model_validate(
                {k: old[k] for k in TicketMessage.model_fields}
            )
        if row["state"] != "open":
            raise Conflict("Ticket is closed")
        count = (
            await _execute(
                session,
                select(func.count())
                .select_from(_SupportMessage)
                .where(
                    _SupportMessage.case_id == case_id,
                    _SupportMessage.author_role == role,
                    _SupportMessage.created_at
                    > await _now(session) - timedelta(minutes=1),
                ),
            )
        ).scalar_one()
        if count >= 20:
            raise RateLimited("Ticket message limit reached")
        message = await _one(
            session,
            insert(_SupportMessage)
            .values(
                case_id=case_id,
                author_role=role,
                author_login=login,
                client_message_id=body.client_message_id,
                text=body.text,
            )
            .returning(*_SupportMessage.__table__.c),
        )
        return TicketMessage.model_validate(
            {k: message[k] for k in TicketMessage.model_fields}
        )


async def _close_ticket(identity, case_id):
    async with _transaction() as session:
        row = await _owned_case(session, identity, case_id, admin=True, lock=True)
        if row["state"] == "open":
            row = await _one(
                session,
                update(_SupportCase)
                .where(_SupportCase.case_id == case_id)
                .values(state="closed", closed_at=await _now(session))
                .returning(*_SupportCase.__table__.c),
            )
        elif row["state"] != "closed":
            raise NotFound("Ticket not found")
        return _case(row)


async def _users(identity, limit, after):
    _admin(identity)
    async with _transaction() as session:
        rows = await _all(
            session,
            select(_Employee)
            .where(_Employee.username > after)
            .order_by(_Employee.username)
            .limit(limit + 1),
        )
        return {
            "items": [AccessRecord(**row) for row in rows[:limit]],
            "next_cursor": rows[limit - 1]["username"] if len(rows) > limit else None,
        }


async def _set_access(identity, username, enabled):
    _admin(identity)
    async with _transaction() as session:
        await _lock(session, "username", username)
        row = await session.get(_Employee, username)
        if row is None:
            session.add(_Employee(username=username, enabled=enabled))
        else:
            row.enabled = enabled
        return AccessRecord(username=username, enabled=enabled)


async def _metrics(identity, from_at, to_at):
    _admin(identity)
    from .support import _metrics_snapshot

    async with _transaction() as session:
        result = await _metrics_snapshot(session, from_at, to_at)
        rows = await _all(
            session,
            select(_Request.received_at, _WebRequest.completed_at)
            .join(_WebRequest, _Request.request_id == _WebRequest.request_id)
            .where(
                _Request.received_at >= from_at,
                _Request.received_at < to_at,
                _WebRequest.completed_at.is_not(None),
            ),
        )
        dissatisfied = (
            await _execute(
                session,
                select(func.count())
                .select_from(_SupportCase)
                .join(_Request, _Request.request_id == _SupportCase.request_id)
                .where(
                    _Request.channel == "web",
                    _Request.received_at >= from_at,
                    _Request.received_at < to_at,
                    _SupportCase.reason == "dissatisfied",
                ),
            )
        ).scalar_one()
    # Availability is a separate measurement, never a Telegram delivery receipt.
    return {
        **result.model_dump(),
        "dissatisfied": result.dissatisfied + dissatisfied,
        "web_result_duration_ms": [
            max(
                0,
                int((row["completed_at"] - row["received_at"]).total_seconds() * 1000),
            )
            for row in rows
        ],
    }
