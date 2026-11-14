"""Short SQLAlchemy transactions, advisory locks and strict JSON boundaries."""

import hashlib
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from rag.contracts import StorageError
from rag.runtime._state import _get
from rag.runtime import _watchdog


def _json(value):
    if hasattr(value, "model_dump_json"):
        return value.model_dump_json()
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _decode(annotation, value):
    try:
        raw = value if isinstance(value, (str, bytes)) else _json(value)
        return TypeAdapter(annotation).validate_json(raw)
    except (ValidationError, ValueError, TypeError) as exc:
        raise StorageError("Invalid stored DTO") from exc


def _data(value):
    return json.loads(value) if isinstance(value, (str, bytes)) else value


async def _question_row(connection, request_id):
    row = await _one(connection, "SELECT * FROM rag_requests WHERE request_id=:id", id=request_id)
    if row is None:
        return None
    await _lock(connection, "user", row["user_id"])
    return await _one(connection, "SELECT * FROM rag_requests WHERE request_id=:id FOR UPDATE", id=request_id)


async def _delivery_row(connection, delivery_id):
    row = await _one(connection, "SELECT * FROM rag_deliveries WHERE delivery_id=:id", id=delivery_id)
    if row is None:
        return None
    await _lock(connection, "user", row["user_id"])
    await _lock(connection, "chat", row["chat_id"])
    return await _one(connection, "SELECT * FROM rag_deliveries WHERE delivery_id=:id FOR UPDATE", id=delivery_id)


async def _prior_update(connection, update_id):
    from rag.rag_api.contracts import IntakeResult
    await _lock(connection, "update", update_id)
    row = await _one(connection, "SELECT result FROM telegram_updates WHERE update_id=:id", id=update_id)
    if row is None:
        return None
    result = _decode(IntakeResult, row["result"])
    if result.kind == "accepted":
        return IntakeResult(kind="duplicate", request_id=result.request_id, delivery_id=result.delivery_id)
    return result


async def _save_update(connection, update_id, user_id, chat_id, received_at, result, action=None):
    await _execute(connection, """INSERT INTO telegram_updates
        (update_id,user_id,chat_id,received_at,result,action)
        VALUES(:update,:user,:chat,:at,CAST(:result AS jsonb),CAST(:action AS jsonb))""",
        update=update_id, user=user_id, chat=chat_id, at=received_at, result=_json(result),
        action=_json(action) if action is not None else None)


@asynccontextmanager
async def _transaction():
    try:
        resources = _get()
        if resources.engine is None:
            raise StorageError("PostgreSQL is not configured")
        with _watchdog._step(resources.settings.io_timeout_s, "postgresql"):
            async with asyncio.timeout(resources.settings.io_timeout_s):
                async with resources.engine.begin() as connection:
                    yield connection
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        # A failed COMMIT response does not prove that the COMMIT was rolled back.
        raise StorageError("PostgreSQL operation was not confirmed") from exc


async def _execute(connection, sql, **params):
    return await connection.execute(text(sql), params)


async def _one(connection, sql, **params):
    return (await _execute(connection, sql, **params)).mappings().first()


async def _all(connection, sql, **params):
    return (await _execute(connection, sql, **params)).mappings().all()


async def _lock(connection, namespace, value):
    digest = hashlib.sha256(f"{namespace}:{value}".encode()).digest()[:8]
    key = int.from_bytes(digest, "big", signed=True)
    await _execute(connection, "SELECT pg_advisory_xact_lock(:key)", key=key)


async def _now(connection):
    return (await _execute(connection, "SELECT clock_timestamp()")).scalar_one()


async def _corpus(connection):
    return await _one(connection, "SELECT * FROM corpus_state WHERE singleton_id=1 FOR UPDATE")


async def _outbox(connection, route, identifier, available_at=None):
    field = {"rag.questions": "request_id", "rag.results": "delivery_id", "ingestion.jobs": "revision_id"}[route]
    await _execute(connection, f"""INSERT INTO outbox(message_id,route,{field},available_at)
        VALUES (:message_id,:route,:identifier,:available_at)""",
        message_id=uuid4(), route=route, identifier=identifier,
        available_at=available_at or datetime.now(UTC))


async def _delivery(connection, *, user_id, chat_id, purpose, request_id=None,
                    case_id=None, content=None, terminal=False, key=None):
    # Callers serialize by employee/chat before allocating an ordering ID.
    await _lock(connection, "chat", chat_id)
    row = await _one(connection, """INSERT INTO rag_deliveries
        (user_id,chat_id,request_id,case_id,purpose,content,terminal_for_request,dedupe_key)
        VALUES (:user_id,:chat_id,:request_id,:case_id,:purpose,CAST(:content AS jsonb),:terminal,:key)
        ON CONFLICT (dedupe_key) DO NOTHING RETURNING delivery_id""",
        user_id=user_id, chat_id=chat_id, request_id=request_id, case_id=case_id,
        purpose=purpose, content=_json(content) if content is not None else None,
        terminal=terminal, key=key)
    if row:
        await _outbox(connection, "rag.results", row["delivery_id"])
        return row["delivery_id"]
    return (await _one(connection, "SELECT delivery_id FROM rag_deliveries WHERE dedupe_key=:key", key=key))["delivery_id"]


async def _wake_delivery(connection, chat_id):
    row = await _one(connection, """SELECT delivery_id FROM rag_deliveries WHERE chat_id=:chat
        AND state IN ('waiting','sending') ORDER BY delivery_id LIMIT 1""", chat=chat_id)
    if row:
        await _outbox(connection, "rag.results", row["delivery_id"])


async def _wake_ingestion(connection, document_id):
    row = await _one(connection, """SELECT revision_id FROM document_revisions
        WHERE document_id=:document AND state IN ('queued','indexing') ORDER BY revision_no LIMIT 1""",
        document=document_id)
    if row:
        await _outbox(connection, "ingestion.jobs", row["revision_id"])


def _valid_attempt(row, attempt, state, now):
    return (row is not None and row["state"] == state and row["attempt_no"] == attempt.attempt_no
            and row["resume_at"] == attempt.deadline_at and row["resume_at"] > now)
