"""Durable idempotency belongs to Runtime, not the model/provider module."""

import os
import re
import socket
from uuid import UUID, uuid4

from sqlalchemy import select
from rag.contracts import StepError
from rag.model_gateway.providers import CallCoordinator
from rag.runtime.events import _context
from rag.runtime.postgresql._db import _transaction, _lock, _now
from rag.runtime.postgresql._models import _ModelCall
from rag.runtime._validation import _positive


class _PostgresModelCalls(CallCoordinator):
    def namespace(self):
        context = _context.get()
        if "request_id" in context:
            return "request:" + str(context["request_id"])
        if "job_id" in context:
            return "job:" + str(context["job_id"])
        return "standalone"

    async def acquire(self, key, profile_id, role, operation):
        async with _transaction() as session:
            await _lock(session, "model_call", key)
            row = await session.get(_ModelCall, key, with_for_update=True)
            if row is not None:
                if row.state == "completed":
                    return dict(kind="done", value=row.result)
                return dict(kind="uncertain" if row.state == "uncertain" else "busy")
            token, now = uuid4(), await _now(session)
            session.add(
                _ModelCall(
                    call_key=key,
                    owner_id=token,
                    owner_pid=os.getpid(),
                    owner_host=socket.gethostname(),
                    work_ref=self.namespace(),
                    profile_id=profile_id,
                    model_role=role,
                    operation=operation,
                    state="executing",
                    result=None,
                    created_at=now,
                    updated_at=now,
                )
            )
            return dict(kind="owned", token=str(token))

    async def complete(self, key, token, value):
        async with _transaction() as session:
            row = await session.get(_ModelCall, key, with_for_update=True)
            if row is None or row.owner_id != UUID(token) or row.state != "executing":
                raise StepError("model_unavailable", True)
            row.state, row.result, row.updated_at = (
                "completed",
                value,
                await _now(session),
            )

    async def release(self, key, token, *, safe):
        async with _transaction() as session:
            row = await session.get(_ModelCall, key, with_for_update=True)
            if (
                row is not None
                and row.owner_id == UUID(token)
                and row.state == "executing"
            ):
                if safe:
                    await session.delete(row)
                else:
                    row.state, row.updated_at = "uncertain", await _now(session)


async def _pending(limit):
    _positive(limit)
    async with _transaction() as session:
        rows = (
            await session.scalars(
                select(_ModelCall)
                .where(_ModelCall.state != "completed")
                .order_by(_ModelCall.created_at)
                .limit(limit)
            )
        ).all()
        return [
            dict(
                call_key=row.call_key,
                state=row.state,
                profile_id=row.profile_id,
                model_role=row.model_role,
                operation=row.operation,
                work_ref=row.work_ref,
                owner_id=str(row.owner_id),
                owner_pid=row.owner_pid,
                owner_host=row.owner_host,
                created_at=row.created_at.isoformat(),
            )
            for row in rows
        ]


async def _reset(key, writers_stopped):
    if not writers_stopped or not re.fullmatch(r"[0-9a-f]{64}", key):
        raise ValueError(
            "Reset requires a call key and confirmed stopped model writers"
        )
    async with _transaction() as session:
        row = await session.get(_ModelCall, key, with_for_update=True)
        if row is None or row.state == "completed":
            return False
        await session.delete(row)
        return True
