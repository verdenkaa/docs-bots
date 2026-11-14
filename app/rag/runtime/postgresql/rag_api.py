"""Contracts: docs/documents/infrastructure/postgresql/rag-api.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from rag.contracts import DeliveryAttempt, RagResult
from rag.rag_api.contracts import ClaimedDelivery, DeliveryPart, IntakeResult, NewQuestion

from datetime import timedelta
from rag.rag_api.contracts import SupportCase
from rag.runtime._state import _get
from rag.runtime._validation import _positive, _utc
from ._db import (_transaction, _one, _all, _execute, _lock, _now, _delivery,
                  _outbox, _decode, _json, _data, _valid_attempt, _delivery_row,
                  _prior_update, _save_update, _wake_delivery)


async def register_question(question: NewQuestion) -> IntakeResult:
    """Атомарно проверить повтор, допуск и активный слот; сохранить вопрос и outbox.

    Args:
        question: Проверенный личный чат, отправитель, вход и время.

    Returns:
        IntakeResult.
    """
    question = NewQuestion.model_validate_json(question.model_dump_json())
    async with _transaction() as connection:
        previous = await _prior_update(connection, question.update_id)
        if previous is not None:
            return previous
        await _lock(connection, "user", question.user_id)
        access = await _one(connection, "SELECT enabled FROM employees WHERE user_id=:id", id=question.user_id)
        if access is None or not access["enabled"]:
            kind = "denied"
        elif await _one(connection, "SELECT request_id FROM rag_requests WHERE user_id=:id AND state!='finished'",
                        id=question.user_id):
            kind = "busy"
        else:
            await _lock(connection, "admission", 0)
            count = (await _execute(connection, "SELECT count(*) FROM rag_requests WHERE state!='finished'")).scalar_one()
            if count >= _get().settings.max_pending_requests:
                return IntakeResult(kind="retry_later")
            row = await _one(connection, """INSERT INTO rag_requests(user_id,chat_id,update_id,input,received_at)
                VALUES(:user,:chat,:update,CAST(:input AS jsonb),:at) RETURNING request_id""",
                user=question.user_id, chat=question.chat_id, update=question.update_id,
                input=_json(question.input), at=question.received_at)
            result = IntakeResult(kind="accepted", request_id=row["request_id"])
            await _outbox(connection, "rag.questions", row["request_id"])
            await _save_update(connection, question.update_id, question.user_id, question.chat_id, question.received_at, result)
            return result
        delivery = await _delivery(connection, user_id=question.user_id, chat_id=question.chat_id,
            purpose=kind, key="update:" + str(question.update_id))
        result = IntakeResult(kind=kind, delivery_id=delivery)
        await _save_update(connection, question.update_id, question.user_id, question.chat_id, question.received_at, result)
        return result


async def record_update(update_id: int, user_id: int | None, chat_id: int | None, received_at: datetime, kind: Literal['ignored', 'denied', 'invalid_update']) -> IntakeResult:
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
    if type(update_id) is not int or update_id < 0 or kind not in ("ignored", "denied", "invalid_update"):
        raise ValueError("Invalid update")
    for value in (user_id, chat_id):
        if value is not None:
            _positive(value)
    async with _transaction() as connection:
        previous = await _prior_update(connection, update_id)
        if previous is not None:
            return previous
        delivery = None
        if kind == "denied" and user_id is not None and chat_id is not None:
            await _lock(connection, "user", user_id)
            delivery = await _delivery(connection, user_id=user_id, chat_id=chat_id, purpose=kind, key="update:" + str(update_id))
        result = IntakeResult(kind=kind, delivery_id=delivery)
        await _save_update(connection, update_id, user_id, chat_id, received_at, result)
        return result


async def claim_delivery(delivery_id: int) -> ClaimedDelivery | None:
    """Взять готовую самостоятельную доставку и выдать отдельную DeliveryAttempt.

    Args:
        delivery_id: Ссылка из DeliveryRef.

    Returns:
        ClaimedDelivery или None.
    """
    _positive(delivery_id)
    async with _transaction() as connection:
        row = await _delivery_row(connection, delivery_id)
        if row is None or row["state"] not in ("waiting", "sending"):
            return None
        first = await _one(connection, """SELECT delivery_id FROM rag_deliveries WHERE chat_id=:chat
            AND state IN ('waiting','sending') ORDER BY delivery_id LIMIT 1""", chat=row["chat_id"])
        if first["delivery_id"] != delivery_id:
            return None
        now = await _now(connection)
        if row["resume_at"] is not None and row["resume_at"] > now:
            return None
        case = None
        stale = False
        if row["purpose"] == "status":
            request = await _one(connection, "SELECT result FROM rag_requests WHERE request_id=:id", id=row["request_id"])
            stale = request is None or request["result"] is not None
        if row["case_id"] is not None:
            case_row = await _one(connection, "SELECT * FROM support_cases WHERE case_id=:id", id=row["case_id"])
            if case_row:
                case = _decode(SupportCase, {key: case_row[key] for key in SupportCase.model_fields})
            if row["purpose"] == "support_confirmation":
                stale = case is None or case.state != "awaiting_confirmation"
        if row["purpose"] == "support_offer":
            existing = await _one(connection, "SELECT state FROM support_cases WHERE request_id=:id", id=row["request_id"])
            stale = existing is not None
        if stale:
            await _execute(connection, "UPDATE rag_deliveries SET state='cancelled' WHERE delivery_id=:id", id=delivery_id)
            await _wake_delivery(connection, row["chat_id"])
            return None
        deadline = now + timedelta(seconds=_get().settings.delivery_timeout_s)
        number = row["attempt_no"] + 1
        await _execute(connection, """UPDATE rag_deliveries SET state='sending',attempt_no=:number,
            resume_at=:deadline,delivery_uncertain=delivery_uncertain OR :uncertain WHERE delivery_id=:id""",
            number=number, deadline=deadline, uncertain=row["state"] == "sending", id=delivery_id)
        return ClaimedDelivery(attempt=DeliveryAttempt(delivery_id=delivery_id,attempt_no=number,deadline_at=deadline),
            user_id=row["user_id"], chat_id=row["chat_id"], request_id=row["request_id"], purpose=row["purpose"],
            content=_decode(RagResult,row["content"]) if row["content"] is not None else None,
            case=case, parts=_decode(list[DeliveryPart],row["parts"]) if row["parts"] is not None else None,
            next_part=row["next_part"])


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
    async with _transaction() as connection:
        row = await _delivery_row(connection, attempt.delivery_id)
        if not _valid_attempt(row, attempt, "sending", await _now(connection)):
            return False
        plan = [part.model_dump(mode="json") for part in parts]
        if row["parts"] is not None and _data(row["parts"]) != plan:
            raise ValueError("Delivery plan is immutable")
        await _execute(connection, "UPDATE rag_deliveries SET parts=CAST(:parts AS jsonb) WHERE delivery_id=:id",
                       parts=_json(plan), id=attempt.delivery_id)
        return True


async def record_delivery_part(attempt: DeliveryAttempt, part_no: int, message_id: int, variant: Literal['normal', 'length_fallback']) -> bool:
    """Сохранить квитанцию одной части и перейти к следующей неподтверждённой.

    Args:
        attempt: Право и индекс текущей части.
        part_no: Право и индекс текущей части.
        message_id: Реальная квитанция Telegram.
        variant: Реальная квитанция Telegram.

    Returns:
        bool.
    """
    if type(part_no) is not int or part_no < 0 or variant not in ("normal", "length_fallback"):
        raise ValueError("Invalid receipt")
    _positive(message_id)
    async with _transaction() as connection:
        row = await _delivery_row(connection, attempt.delivery_id)
        now = await _now(connection)
        if not _valid_attempt(row, attempt, "sending", now):
            return False
        plan, receipts = _data(row["parts"]), _data(row["receipts"])
        if plan is None or part_no >= len(plan):
            raise ValueError("Receipt has no planned part")
        if part_no < row["next_part"]:
            old = receipts[part_no]
            if old["message_id"] != message_id or old["variant"] != variant:
                raise ValueError("Conflicting receipt")
            return True
        if part_no != row["next_part"]:
            raise ValueError("Delivery parts must be acknowledged in order")
        receipts.append({"part_no":part_no,"message_id":message_id,"variant":variant,"confirmed_at":now.isoformat()})
        await _execute(connection, """UPDATE rag_deliveries SET receipts=CAST(:receipts AS jsonb),
            next_part=next_part+1 WHERE delivery_id=:id""", receipts=_json(receipts), id=attempt.delivery_id)
        return True


async def schedule_delivery_retry(attempt: DeliveryAttempt, retry_at: datetime, uncertain: bool) -> bool:
    """Сохранить время повтора доставки и outbox rag.results.

    Args:
        attempt: Текущая попытка доставки.
        retry_at: Следующий запуск и возможная отправка текущей части.
        uncertain: Следующий запуск и возможная отправка текущей части.

    Returns:
        bool.
    """
    _utc(retry_at)
    async with _transaction() as connection:
        row = await _delivery_row(connection, attempt.delivery_id)
        if not _valid_attempt(row, attempt, "sending", await _now(connection)):
            return False
        await _execute(connection, """UPDATE rag_deliveries SET state='waiting',resume_at=:at,
            delivery_uncertain=delivery_uncertain OR :uncertain WHERE delivery_id=:id""",
            at=retry_at, uncertain=uncertain, id=attempt.delivery_id)
        await _outbox(connection, "rag.results", attempt.delivery_id, retry_at)
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
    async with _transaction() as connection:
        row = await _delivery_row(connection, attempt.delivery_id)
        if not _valid_attempt(row, attempt, "sending", await _now(connection)):
            return False
        if delivered and (row["parts"] is None or row["next_part"] != len(_data(row["parts"]))):
            raise ValueError("Cannot complete delivery before all receipts")
        state = "delivered" if delivered else ("unknown" if uncertain or row["delivery_uncertain"] else "failed")
        await _execute(connection, """UPDATE rag_deliveries SET state=:state,resume_at=NULL,
            delivery_uncertain=delivery_uncertain OR :uncertain WHERE delivery_id=:id""",
            state=state, uncertain=uncertain, id=attempt.delivery_id)
        if row["terminal_for_request"]:
            await _execute(connection, "UPDATE rag_requests SET state='finished' WHERE request_id=:id", id=row["request_id"])
        await _wake_delivery(connection, row["chat_id"])
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
    async with _transaction() as connection:
        rows = await _all(connection, """SELECT request_id FROM rag_requests r
            WHERE result IS NULL AND received_at<=:before AND NOT EXISTS
            (SELECT 1 FROM rag_deliveries d WHERE d.request_id=r.request_id AND purpose='status')
            ORDER BY request_id LIMIT :limit""", before=now-timedelta(seconds=10), limit=limit)
    count = 0
    for item in rows:
        # Never hold several employees' locks in a batch; Access locks by user ID.
        async with _transaction() as connection:
            from ._db import _question_row
            row = await _question_row(connection,item["request_id"])
            if row is not None and row["result"] is None:
                exists = await _one(connection,"SELECT delivery_id FROM rag_deliveries WHERE request_id=:id AND purpose='status'",id=row["request_id"])
                if exists is None:
                    await _delivery(connection,user_id=row["user_id"],chat_id=row["chat_id"],request_id=row["request_id"],
                        purpose="status",key="status:"+str(row["request_id"]))
                    count += 1
    return count
