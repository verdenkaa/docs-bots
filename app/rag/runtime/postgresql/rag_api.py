"""Contracts: docs/documents/infrastructure/postgresql/rag-api.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from rag.contracts import DeliveryAttempt, RagResult
from rag.rag_api.contracts import ClaimedDelivery, DeliveryPart, IntakeResult, NewQuestion, TextPart

from datetime import timedelta
from rag.rag_api.contracts import SupportCase
from rag.runtime._state import _get
from rag.runtime._validation import _positive, _utc
from ._db import (
    _transaction,
    _one,
    _all,
    _execute,
    _lock,
    _now,
    _delivery,
    _outbox,
    _decode,
    _json,
    _data,
    _valid_attempt,
    _delivery_row,
    _prior_update,
    _save_update,
    _wake_delivery,
)

from sqlalchemy import func, insert, or_, select, update
from ._models import _Delivery, _Employee, _Request, _SupportCase


async def check_admission_ready() -> bool:
    """Проверить ёмкость и реальные записи вопроса/outbox, откатив пробу.

    Returns:
        True при готовности; ошибка хранилища не разрешает приём.
    """
    from uuid import uuid4

    async with _transaction() as session:
        await _lock(session, "admission", 0)
        count = (await _execute(session, select(func.count()).select_from(_Request).where(
            _Request.state != "finished"))).scalar_one()
        if count >= _get().settings.max_pending_requests:
            return False
        # Exercise the same constraints, privileges and sequences as admission.
        # The savepoint is always rolled back; no probe can reach the relay.
        probe = await session.begin_nested()
        try:
            await _execute(session, select(_Employee.username).limit(0))
            identity = -(uuid4().int % (2 ** 62) + 1)
            row = await _one(session, insert(_Request).values(
                user_id=identity, chat_id=identity, update_id=identity,
                input={"kind": "text", "text": "Readiness probe"},
                received_at=func.clock_timestamp(), state="finished").returning(_Request.request_id))
            await _outbox(session, "rag.questions", row["request_id"])
            await _save_update(session, -identity, identity, identity, await _now(session),
                               IntakeResult(kind="ignored"))
        finally:
            await probe.rollback()
        return True


async def register_question(question: NewQuestion) -> IntakeResult:
    """Атомарно проверить повтор, допуск и активный слот; сохранить вопрос и outbox.

    Args:
        question: Проверенный личный чат, отправитель, вход и время.

    Returns:
        IntakeResult.
    """
    question = NewQuestion.model_validate_json(question.model_dump_json())
    async with _transaction() as session:
        previous = await _prior_update(session, question.update_id)
        if previous is not None:
            return previous
        if question.username is not None:
            await _lock(session, "username", question.username)
        access = await _one(
            session, select(_Employee.enabled).where(_Employee.username == question.username)
        )
        await _lock(session, "user", question.user_id)
        if access is None or not access["enabled"]:
            kind = "denied"
        elif await _one(
            session,
            select(_Request.request_id).where(
                _Request.user_id == question.user_id, _Request.state != "finished"
            ),
        ):
            kind = "busy"
        else:
            await _lock(session, "admission", 0)
            count = (
                await _execute(
                    session,
                    select(func.count()).select_from(_Request).where(_Request.state != "finished"),
                )
            ).scalar_one()
            if count >= _get().settings.max_pending_requests:
                return IntakeResult(kind="retry_later")
            row = await _one(
                session,
                insert(_Request)
                .values(
                    user_id=question.user_id,
                    chat_id=question.chat_id,
                    username=question.username,
                    update_id=question.update_id,
                    input=_data(_json(question.input)),
                    received_at=question.received_at,
                )
                .returning(_Request.request_id),
            )
            result = IntakeResult(kind="accepted", request_id=row["request_id"])
            await _outbox(session, "rag.questions", row["request_id"])
            await _save_update(
                session,
                question.update_id,
                question.user_id,
                question.chat_id,
                question.received_at,
                result,
            )
            return result
        delivery = await _delivery(
            session,
            user_id=question.user_id,
            chat_id=question.chat_id,
            purpose=kind,
            key="update:" + str(question.update_id),
        )
        result = IntakeResult(kind=kind, delivery_id=delivery)
        await _save_update(
            session,
            question.update_id,
            question.user_id,
            question.chat_id,
            question.received_at,
            result,
        )
        return result


async def record_update(
    update_id: int,
    user_id: int | None,
    chat_id: int | None,
    received_at: datetime,
    kind: Literal["ignored", "denied", "invalid_update"],
) -> IntakeResult:
    """Сохранить обработанный update и окончательный исход без нового RAG-вопроса.

    Args:
        update_id: Идентичность и время обработанного update.
        user_id: Получатель, если достоверно известен личный чат.
        chat_id: Получатель, если достоверно известен личный чат.
        received_at: Идентичность и время обработанного update.
        kind: Выбранный RAG API окончательный исход.

    Returns:
        IntakeResult.
    """
    _utc(received_at)
    if (
        type(update_id) is not int
        or update_id < 0
        or kind not in ("ignored", "denied", "invalid_update")
    ):
        raise ValueError("Invalid update")
    for value in (user_id, chat_id):
        if value is not None:
            _positive(value)
    async with _transaction() as session:
        previous = await _prior_update(session, update_id)
        if previous is not None:
            return previous
        delivery = None
        if kind == "denied" and user_id is not None and chat_id is not None:
            await _lock(session, "user", user_id)
            delivery = await _delivery(
                session,
                user_id=user_id,
                chat_id=chat_id,
                purpose=kind,
                key="update:" + str(update_id),
            )
        result = IntakeResult(kind=kind, delivery_id=delivery)
        await _save_update(session, update_id, user_id, chat_id, received_at, result)
        return result


async def claim_delivery(delivery_id: int) -> ClaimedDelivery | None:
    """Взять готовую самостоятельную доставку и выдать отдельную DeliveryAttempt.

    Args:
        delivery_id: Ссылка из DeliveryRef.

    Returns:
        ClaimedDelivery или None.
    """
    _positive(delivery_id)
    async with _transaction() as session:
        row = await _delivery_row(session, delivery_id)
        if row is None or row["state"] not in ("waiting", "sending"):
            return None
        first = await _one(
            session,
            select(_Delivery.delivery_id)
            .where(_Delivery.chat_id == row["chat_id"], _Delivery.state.in_(("waiting", "sending")))
            .order_by(_Delivery.delivery_id)
            .limit(1),
        )
        if first["delivery_id"] != delivery_id:
            return None
        now = await _now(session)
        if row["resume_at"] is not None and row["resume_at"] > now:
            return None
        case = None
        stale = False
        if row["purpose"] == "status":
            request = await _one(
                session, select(_Request.result).where(_Request.request_id == row["request_id"])
            )
            stale = request is None or request["result"] is not None
        if row["case_id"] is not None:
            case_row = await _one(
                session, select(_SupportCase).where(_SupportCase.case_id == row["case_id"])
            )
            if case_row:
                case = SupportCase.model_validate(
                    {key: case_row[key] for key in SupportCase.model_fields}
                )
            if row["purpose"] == "support_confirmation":
                stale = case is None or case.state != "awaiting_confirmation"
        if row["purpose"] == "support_offer":
            existing = await _one(
                session,
                select(_SupportCase.state).where(_SupportCase.request_id == row["request_id"]),
            )
            stale = existing is not None
        if stale:
            await _execute(
                session,
                update(_Delivery)
                .where(_Delivery.delivery_id == delivery_id)
                .values(state="cancelled"),
            )
            await _wake_delivery(session, row["chat_id"])
            return None
        deadline = now + timedelta(seconds=_get().settings.delivery_timeout_s)
        number = row["attempt_no"] + 1
        await _execute(
            session,
            update(_Delivery)
            .where(_Delivery.delivery_id == delivery_id)
            .values(
                state="sending",
                attempt_no=number,
                resume_at=deadline,
                delivery_uncertain=or_(_Delivery.delivery_uncertain, row["state"] == "sending"),
            ),
        )
        from rag.runtime._photos import _deadline

        _deadline.set(deadline)
        return ClaimedDelivery(
            attempt=DeliveryAttempt(
                delivery_id=delivery_id, attempt_no=number, deadline_at=deadline
            ),
            user_id=row["user_id"],
            chat_id=row["chat_id"],
            request_id=row["request_id"],
            purpose=row["purpose"],
            content=_decode(RagResult, row["content"]) if row["content"] is not None else None,
            case=case,
            parts=_decode(list[DeliveryPart], row["parts"]) if row["parts"] is not None else None,
            next_part=row["next_part"],
        )


async def store_delivery_parts(attempt: DeliveryAttempt, parts: Sequence[DeliveryPart]) -> bool:
    """Закрепить неизменяемый план частей перед первой отправкой Telegram.

    Args:
        attempt: Взятая попытка.
        parts: Уже собранный неизменяемый план.

    Returns:
        bool.
    """
    parts = _decode(list[DeliveryPart], _json([part.model_dump(mode="json") for part in parts]))
    if not parts:
        raise ValueError("Delivery plan must not be empty")
    async with _transaction() as session:
        row = await _delivery_row(session, attempt.delivery_id)
        if not _valid_attempt(row, attempt, "sending", await _now(session)):
            return False
        plan = [part.model_dump(mode="json") for part in parts]
        if row["parts"] is not None and _data(row["parts"]) != plan:
            raise ValueError("Delivery plan is immutable")
        await _execute(
            session,
            update(_Delivery)
            .where(_Delivery.delivery_id == attempt.delivery_id)
            .values(parts=_data(_json(plan))),
        )
        return True


async def record_delivery_part(
    attempt: DeliveryAttempt,
    part_no: int,
    message_id: int,
    variant: Literal["normal", "length_fallback"],
    delivered_text: str | None = None,
) -> bool:
    """Сохранить квитанцию одной части и перейти к следующей неподтверждённой.

    Args:
        attempt: Право и индекс текущей части.
        part_no: Право и индекс текущей части.
        message_id: Реальная квитанция Telegram.
        variant: Реальная квитанция Telegram.
        delivered_text: Подтверждённый текст; None для изображения.

    Returns:
        bool.
    """
    if type(part_no) is not int or part_no < 0 or variant not in ("normal", "length_fallback"):
        raise ValueError("Invalid receipt")
    _positive(message_id)
    async with _transaction() as session:
        row = await _delivery_row(session, attempt.delivery_id)
        now = await _now(session)
        if not _valid_attempt(row, attempt, "sending", now):
            return False
        plan, receipts = _data(row["parts"]), _data(row["receipts"])
        if plan is None or part_no >= len(plan):
            raise ValueError("Receipt has no planned part")
        part = plan[part_no]
        if part["kind"] == "text":
            if delivered_text is None and variant == "normal":
                delivered_text = part["text"]
            TextPart(kind="text", text=delivered_text)
            if variant == "normal" and (delivered_text != part["text"]
                                        or part.get("variant", "normal") != "normal"):
                raise ValueError("Receipt differs from planned text")
        elif delivered_text is not None or variant != "normal":
            raise ValueError("Image receipt cannot contain text or fallback")
        if part_no < row["next_part"]:
            old = receipts[part_no]
            if (old["message_id"] != message_id or old["variant"] != variant
                    or old.get("delivered_text") != delivered_text):
                raise ValueError("Conflicting receipt")
            return True
        if part_no != row["next_part"]:
            raise ValueError("Delivery parts must be acknowledged in order")
        receipts.append(
            {
                "part_no": part_no,
                "message_id": message_id,
                "variant": variant,
                "delivered_text": delivered_text,
                "confirmed_at": now.isoformat(),
            }
        )
        await _execute(
            session,
            update(_Delivery)
            .where(_Delivery.delivery_id == attempt.delivery_id)
            .values(receipts=_data(_json(receipts)), next_part=_Delivery.next_part + 1),
        )
        return True


async def schedule_delivery_retry(
    attempt: DeliveryAttempt, retry_at: datetime, uncertain: bool
) -> bool:
    """Сохранить время повтора доставки и outbox rag.results.

    Args:
        attempt: Текущая попытка доставки.
        retry_at: Следующий запуск и возможная отправка текущей части.
        uncertain: Следующий запуск и возможная отправка текущей части.

    Returns:
        bool.
    """
    _utc(retry_at)
    async with _transaction() as session:
        row = await _delivery_row(session, attempt.delivery_id)
        if not _valid_attempt(row, attempt, "sending", await _now(session)):
            return False
        await _execute(
            session,
            update(_Delivery)
            .where(_Delivery.delivery_id == attempt.delivery_id)
            .values(
                state="waiting",
                resume_at=retry_at,
                delivery_uncertain=or_(_Delivery.delivery_uncertain, uncertain),
            ),
        )
        await _outbox(session, "rag.results", attempt.delivery_id, retry_at)
        return True


async def complete_delivery(attempt: DeliveryAttempt, delivered: bool, uncertain: bool) -> bool:
    """Завершить доставку и разблокировать следующий ответ чата.

    Args:
        attempt: Текущая попытка.
        delivered: Все части подтверждены / хотя бы одна могла дойти.
        uncertain: Все части подтверждены / хотя бы одна могла дойти.

    Returns:
        bool.
    """
    async with _transaction() as session:
        row = await _delivery_row(session, attempt.delivery_id)
        if not _valid_attempt(row, attempt, "sending", await _now(session)):
            return False
        if delivered and (row["parts"] is None or row["next_part"] != len(_data(row["parts"]))):
            raise ValueError("Cannot complete delivery before all receipts")
        state = (
            "delivered"
            if delivered
            else ("unknown" if uncertain or row["delivery_uncertain"] else "failed")
        )
        await _execute(
            session,
            update(_Delivery)
            .where(_Delivery.delivery_id == attempt.delivery_id)
            .values(
                state=state,
                resume_at=None,
                delivery_uncertain=or_(_Delivery.delivery_uncertain, uncertain),
            ),
        )
        if row["terminal_for_request"]:
            await _execute(
                session,
                update(_Request)
                .where(_Request.request_id == row["request_id"])
                .values(state="finished"),
            )
        await _wake_delivery(session, row["chat_id"])
        return True


async def queue_waiting_statuses(now: datetime, limit: int) -> int:
    """Поставить актуальные статусы после 10 секунд ожидания без готового итога.

    Args:
        now: Момент и предел пачки.
        limit: Момент и предел пачки.

    Returns:
        int.
    """
    _utc(now)
    _positive(limit)
    async with _transaction() as session:
        rows = await _all(
            session,
            select(_Request.request_id)
            .where(
                _Request.result.is_(None),
                _Request.received_at <= now - timedelta(seconds=10),
                ~select(_Delivery.delivery_id)
                .where(_Delivery.request_id == _Request.request_id, _Delivery.purpose == "status")
                .exists(),
            )
            .order_by(_Request.request_id)
            .limit(limit),
        )
    count = 0
    for item in rows:
        # Never hold several employees' locks in a batch; Access locks by user ID.
        async with _transaction() as session:
            from ._db import _question_row

            row = await _question_row(session, item["request_id"])
            if row is not None and row["result"] is None:
                exists = await _one(
                    session,
                    select(_Delivery.delivery_id).where(
                        _Delivery.request_id == row["request_id"], _Delivery.purpose == "status"
                    ),
                )
                if exists is None:
                    await _delivery(
                        session,
                        user_id=row["user_id"],
                        chat_id=row["chat_id"],
                        request_id=row["request_id"],
                        purpose="status",
                        key="status:" + str(row["request_id"]),
                    )
                    count += 1
    return count
