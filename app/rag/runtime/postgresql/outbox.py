"""Contracts: docs/documents/infrastructure/postgresql/outbox.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, Field

from rag.dto import DTO
from rag.runtime.rabbitmq import OutboxNotice, WorkRoute

from datetime import timedelta
from rag.runtime._state import _get
from rag.runtime._validation import _positive
from rag.runtime.rabbitmq import QuestionNotice, ResultNotice, IngestionNotice
from rag.contracts import RequestRef, DeliveryRef
from rag.ingestion.contracts import JobRef
from ._db import _transaction, _one, _all, _now, _outbox


class ReadyOutboxRow(DTO):
    """Внутренняя запись для Relay: сообщение и время готовности для диагностики.

    available_at не требуется добавлять в сообщение RabbitMQ.
    """
    notice: OutboxNotice = Field(description='Уведомление, передаваемое Relay в publish_notice.')
    available_at: AwareDatetime = Field(description='Сохранённое время готовности outbox в UTC для расчёта ожидания публикации.')


async def list_ready_outbox(limit: int) -> list[ReadyOutboxRow]:
    """Прочитать неопубликованные уведомления, для которых наступил available_at.

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список объектов Pydantic `ReadyOutboxRow`. `[]` допустим.
    """
    _positive(limit)
    async with _transaction() as connection:
        rows = await _all(connection,"""SELECT * FROM outbox WHERE published_at IS NULL
            AND available_at<=clock_timestamp() ORDER BY available_at,message_id LIMIT :limit""",limit=limit)
        result = []
        for row in rows:
            common = dict(message_id=row["message_id"],route=row["route"])
            if row["route"]=="rag.questions":
                notice = QuestionNotice(**common,event=RequestRef(request_id=row["request_id"]))
            elif row["route"]=="rag.results":
                notice = ResultNotice(**common,event=DeliveryRef(delivery_id=row["delivery_id"]))
            else:
                notice = IngestionNotice(**common,event=JobRef(job_id=row["revision_id"]))
            result.append(ReadyOutboxRow(notice=notice,available_at=row["available_at"]))
        return result


async def mark_outbox_published(message_id: UUID) -> bool:
    """Отметить публикацию после подтверждения маршрутизации брокером.

    Args:
        message_id: ID из опубликованного OutboxNotice.

    Returns:
        Одно логическое значение.
    """
    async with _transaction() as connection:
        row = await _one(connection,"""UPDATE outbox SET published_at=clock_timestamp()
            WHERE message_id=:id AND published_at IS NULL RETURNING message_id""",id=message_id)
        return row is not None


async def recover_pending_work(route: WorkRoute, limit: int) -> int:
    """Добавить outbox для готовой незавершённой работы с учётом окна доставки.

    Args:
        route: Какой этап восстанавливаем.
        limit: Положительный предел размера пачки.

    Returns:
        Одно целое число.
    """
    _positive(limit)
    options = {
        "rag.questions":("rag_requests","request_id","work.state IN ('queued','processing')","TRUE"),
        "rag.results":("rag_deliveries","delivery_id","work.state IN ('waiting','sending')",
            "NOT EXISTS(SELECT 1 FROM rag_deliveries prior WHERE prior.chat_id=work.chat_id AND prior.delivery_id<work.delivery_id AND prior.state IN ('waiting','sending'))"),
        "ingestion.jobs":("document_revisions","revision_id","work.state IN ('queued','indexing') AND NOT work.qdrant_write_pending",
            "NOT EXISTS(SELECT 1 FROM document_revisions prior WHERE prior.document_id=work.document_id AND prior.revision_no<work.revision_no AND prior.state IN ('queued','indexing'))"),
    }
    if route not in options:
        raise ValueError("Unknown work route")
    table,field,state,ordered = options[route]
    async with _transaction() as connection:
        now = await _now(connection)
        before = now - timedelta(seconds=_get().settings.recovery_window_s)
        rows = await _all(connection,f"""SELECT work.{field} AS id FROM {table} work WHERE {state}
            AND (work.resume_at IS NULL OR work.resume_at<=:now) AND {ordered}
            AND NOT EXISTS(SELECT 1 FROM outbox o WHERE o.route=:route AND o.{field}=work.{field}
            AND (o.published_at IS NULL OR o.published_at>:before))
            ORDER BY work.{field} LIMIT :limit FOR UPDATE OF work SKIP LOCKED""",
            now=now,before=before,route=route,limit=limit)
        for row in rows:
            await _outbox(connection,route,row["id"])
        return len(rows)
