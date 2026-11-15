"""Contracts: docs/documents/infrastructure/rabbitmq/contracts.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from datetime import UTC, datetime
from time import monotonic
from uuid import uuid4
from rag.runtime.postgresql.outbox import list_ready_outbox, mark_outbox_published
from rag.runtime.rabbitmq import publish_notice
from rag.runtime.events import emit_event
from rag.runtime._validation import _positive


async def relay_outbox_batch(limit: int) -> int:
    """Опубликовать готовую пачку outbox и отметить подтверждённые сообщения в БД.

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Одно целое число.
    """
    _positive(limit)
    count = 0
    for row in await list_ready_outbox(limit):
        run_id = str(uuid4())
        started = datetime.now(UTC)
        clock = monotonic()
        fields = dict(route=row.notice.route,message_id=str(row.notice.message_id),
                      available_at=row.available_at.isoformat(),publish_started_at=started.isoformat())
        wait_ms = (started - row.available_at).total_seconds() * 1000
        if wait_ms >= 0:
            fields["outbox_wait_ms"] = round(wait_ms, 3)
        fields.update(row.notice.event.model_dump(mode="json"))
        emit_event("queue_relay","publish_notice","started",run_id,started_at=started.isoformat(),**fields)
        outcome = "failed"
        broker_confirmed = outbox_marked = False
        error_code = None
        try:
            await publish_notice(row.notice)
            broker_confirmed = True
            outbox_marked = await mark_outbox_published(row.notice.message_id)
            if outbox_marked:
                count += 1
            outcome = "published"
        except Exception as exc:
            error_code = getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            emit_event("queue_relay","publish_notice","finished",run_id,**fields,outcome=outcome,
                       broker_confirmed=broker_confirmed, outbox_marked=outbox_marked, error_code=error_code,
                       duration_ms=round((monotonic()-clock)*1000),finished_at=datetime.now(UTC).isoformat())
    return count
