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

from sqlalchemy import and_, func, or_, select, true, update
from sqlalchemy.orm import aliased
from ._models import _Delivery, _Outbox, _Request, _Revision


class ReadyOutboxRow(DTO):
    """Внутренняя запись для Relay: сообщение и время готовности для диагностики.

    available_at не требуется добавлять в сообщение RabbitMQ.
    """

    notice: OutboxNotice = Field(description="Уведомление, передаваемое Relay в publish_notice.")
    available_at: AwareDatetime = Field(
        description="Сохранённое время готовности outbox в UTC для расчёта ожидания публикации."
    )


async def list_ready_outbox(limit: int) -> list[ReadyOutboxRow]:
    """Прочитать неопубликованные уведомления, для которых наступил available_at.

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список объектов Pydantic `ReadyOutboxRow`. `[]` допустим.
    """
    _positive(limit)
    async with _transaction() as session:
        rows = await _all(
            session,
            select(_Outbox)
            .where(_Outbox.published_at.is_(None), _Outbox.available_at <= func.clock_timestamp())
            .order_by(_Outbox.available_at, _Outbox.message_id)
            .limit(limit),
        )
        result = []
        for row in rows:
            common = dict(message_id=row["message_id"], route=row["route"])
            if row["route"] == "rag.questions":
                notice = QuestionNotice(**common, event=RequestRef(request_id=row["request_id"]))
            elif row["route"] == "rag.results":
                notice = ResultNotice(**common, event=DeliveryRef(delivery_id=row["delivery_id"]))
            else:
                notice = IngestionNotice(**common, event=JobRef(job_id=row["revision_id"]))
            result.append(ReadyOutboxRow(notice=notice, available_at=row["available_at"]))
        return result


async def mark_outbox_published(message_id: UUID) -> bool:
    """Отметить публикацию после подтверждения маршрутизации брокером.

    Args:
        message_id: ID из опубликованного OutboxNotice.

    Returns:
        Одно логическое значение.
    """
    async with _transaction() as session:
        row = await _one(
            session,
            update(_Outbox)
            .where(_Outbox.message_id == message_id, _Outbox.published_at.is_(None))
            .values(published_at=func.clock_timestamp())
            .returning(_Outbox.message_id),
        )
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
    prior_delivery = aliased(_Delivery)
    prior_revision = aliased(_Revision)
    delivery_order = (
        ~select(prior_delivery.delivery_id)
        .where(
            prior_delivery.chat_id == _Delivery.chat_id,
            prior_delivery.delivery_id < _Delivery.delivery_id,
            prior_delivery.state.in_(("waiting", "sending")),
        )
        .exists()
    )
    revision_order = (
        ~select(prior_revision.revision_id)
        .where(
            prior_revision.document_id == _Revision.document_id,
            prior_revision.revision_no < _Revision.revision_no,
            prior_revision.state.in_(("queued", "indexing")),
        )
        .exists()
    )
    options = {
        "rag.questions": (
            _Request,
            "request_id",
            _Request.state.in_(("queued", "processing")),
            true(),
        ),
        "rag.results": (
            _Delivery,
            "delivery_id",
            _Delivery.state.in_(("waiting", "sending")),
            delivery_order,
        ),
        "ingestion.jobs": (
            _Revision,
            "revision_id",
            and_(
                _Revision.state.in_(("queued", "indexing")),
                _Revision.qdrant_write_pending.is_(False),
            ),
            revision_order,
        ),
    }
    if route not in options:
        raise ValueError("Unknown work route")
    model, field, state, ordered = options[route]
    identifier = getattr(model, field)
    async with _transaction() as session:
        now = await _now(session)
        before = now - timedelta(seconds=_get().settings.recovery_window_s)
        recent_notice = (
            select(_Outbox.message_id)
            .where(
                _Outbox.route == route,
                getattr(_Outbox, field) == identifier,
                or_(_Outbox.published_at.is_(None), _Outbox.published_at > before),
            )
            .exists()
        )
        rows = await _all(
            session,
            select(identifier.label("id"))
            .where(
                state,
                or_(model.resume_at.is_(None), model.resume_at <= now),
                ordered,
                ~recent_notice,
            )
            .order_by(identifier)
            .limit(limit)
            .with_for_update(of=model, skip_locked=True),
        )
        for row in rows:
            await _outbox(session, route, row["id"])
        return len(rows)
