"""Short SQLAlchemy transactions, advisory locks and strict JSON boundaries."""

import hashlib
import asyncio
import json
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import func, insert, inspect, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from rag.contracts import StorageError
from rag.runtime._state import _get
from rag.runtime import _watchdog
from ._models import _CorpusState, _Delivery, _Outbox, _Request, _Revision, _TelegramUpdate


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


async def _question_row(session, request_id):
    row = await _one(session, select(_Request).where(_Request.request_id == request_id))
    if row is None:
        return None
    await _lock(session, "user", row["user_id"])
    return await _one(
        session, select(_Request).where(_Request.request_id == request_id).with_for_update()
    )


async def _delivery_row(session, delivery_id):
    row = await _one(session, select(_Delivery).where(_Delivery.delivery_id == delivery_id))
    if row is None:
        return None
    await _lock(session, "user", row["user_id"])
    await _lock(session, "chat", row["chat_id"])
    return await _one(
        session, select(_Delivery).where(_Delivery.delivery_id == delivery_id).with_for_update()
    )


async def _prior_update(session, update_id):
    from rag.rag_api.contracts import IntakeResult

    await _lock(session, "update", update_id)
    row = await _one(
        session, select(_TelegramUpdate.result).where(_TelegramUpdate.update_id == update_id)
    )
    if row is None:
        return None
    result = _decode(IntakeResult, row["result"])
    if result.kind == "accepted":
        return IntakeResult(
            kind="duplicate", request_id=result.request_id, delivery_id=result.delivery_id
        )
    return result


async def _save_update(session, update_id, user_id, chat_id, received_at, result, action=None):
    await _execute(
        session,
        insert(_TelegramUpdate).values(
            update_id=update_id,
            user_id=user_id,
            chat_id=chat_id,
            received_at=received_at,
            result=_data(_json(result)),
            action=action,
        ),
    )


@asynccontextmanager
async def _transaction():
    try:
        resources = _get()
        if resources.engine is None:
            raise StorageError("PostgreSQL is not configured")
        with _watchdog._step(resources.settings.io_timeout_s, "postgresql"):
            async with asyncio.timeout(resources.settings.io_timeout_s):
                async with AsyncSession(
                    resources.engine, expire_on_commit=False, autoflush=False
                ) as session:
                    async with session.begin():
                        yield session
    except (SQLAlchemyError, OSError, TimeoutError) as exc:
        # A failed COMMIT response does not prove that the COMMIT was rolled back.
        raise StorageError("PostgreSQL operation was not confirmed") from exc


async def _execute(session, sql, **params):
    if isinstance(sql, str):
        raise TypeError("Repositories require SQLAlchemy statements, not SQL strings")
    statement = sql.execution_options(populate_existing=True, synchronize_session=False)
    return await session.execute(statement, params)


async def _one(session, sql, **params):
    result = await _execute(session, sql, **params)
    if _entity_select(sql):
        entity = result.scalars().first()
        return _row(entity) if entity is not None else None
    return result.mappings().first()


async def _all(session, sql, **params):
    result = await _execute(session, sql, **params)
    if _entity_select(sql):
        return [_row(entity) for entity in result.scalars().all()]
    return result.mappings().all()


def _entity_select(statement):
    descriptions = getattr(statement, "column_descriptions", [])
    return (
        len(descriptions) == 1
        and descriptions[0].get("entity") is not None
        and descriptions[0]["expr"] is descriptions[0]["entity"]
    )


def _row(entity):
    return {
        column.key: getattr(entity, column.key) for column in inspect(entity).mapper.column_attrs
    }


async def _lock(session, namespace, value):
    digest = hashlib.sha256(f"{namespace}:{value}".encode()).digest()[:8]
    key = int.from_bytes(digest, "big", signed=True)
    await _execute(session, select(func.pg_advisory_xact_lock(key)))


async def _now(session):
    return (await _execute(session, select(func.clock_timestamp()))).scalar_one()


async def _corpus(session):
    return await _one(
        session, select(_CorpusState).where(_CorpusState.singleton_id == 1).with_for_update()
    )


async def _outbox(session, route, identifier, available_at=None):
    field = {
        "rag.questions": "request_id",
        "rag.results": "delivery_id",
        "ingestion.jobs": "revision_id",
    }[route]
    await _execute(
        session,
        insert(_Outbox).values(
            message_id=uuid4(),
            route=route,
            available_at=available_at or datetime.now(UTC),
            **{field: identifier},
        ),
    )


async def _delivery(
    session,
    *,
    user_id,
    chat_id,
    purpose,
    request_id=None,
    case_id=None,
    content=None,
    terminal=False,
    key=None,
):
    # Callers serialize by employee/chat before allocating an ordering ID.
    await _lock(session, "chat", chat_id)
    if key is not None:
        existing = await _one(
            session, select(_Delivery.delivery_id).where(_Delivery.dedupe_key == key)
        )
        if existing is not None:
            return existing["delivery_id"]
    row = await _one(
        session,
        insert(_Delivery)
        .values(
            user_id=user_id,
            chat_id=chat_id,
            request_id=request_id,
            case_id=case_id,
            purpose=purpose,
            content=_data(_json(content)) if content is not None else None,
            terminal_for_request=terminal,
            dedupe_key=key,
        )
        .returning(_Delivery.delivery_id),
    )
    await _outbox(session, "rag.results", row["delivery_id"])
    return row["delivery_id"]


async def _wake_delivery(session, chat_id):
    row = await _one(
        session,
        select(_Delivery.delivery_id)
        .where(_Delivery.chat_id == chat_id, _Delivery.state.in_(("waiting", "sending")))
        .order_by(_Delivery.delivery_id)
        .limit(1),
    )
    if row:
        await _outbox(session, "rag.results", row["delivery_id"])


async def _wake_ingestion(session, document_id):
    row = await _one(
        session,
        select(_Revision.revision_id)
        .where(_Revision.document_id == document_id, _Revision.state.in_(("queued", "indexing")))
        .order_by(_Revision.revision_no)
        .limit(1),
    )
    if row:
        await _outbox(session, "ingestion.jobs", row["revision_id"])


def _valid_attempt(row, attempt, state, now):
    return (
        row is not None
        and row["state"] == state
        and row["attempt_no"] == attempt.attempt_no
        and row["resume_at"] == attempt.deadline_at
        and row["resume_at"] > now
    )
