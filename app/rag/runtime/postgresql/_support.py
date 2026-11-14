"""Authorization and snapshots shared by support state transitions."""

from rag.contracts import QuestionInput, QuestionOutcome
import hmac

from pydantic import TypeAdapter
from rag.rag_api.contracts import (
    AccessDenied,
    HistoryEntry,
    NotFound,
    Principal,
    SupportCase,
    TelegramUsername,
)
from rag.runtime._state import _get
from ._db import _one, _all, _decode, _data

from sqlalchemy import select
from ._models import _Delivery, _Request, _SupportCase


async def _actor(session, actor):
    # Authentication is the trusted HTTP adapter's responsibility. A Principal
    # is an internal capability, never an HTTP body accepted by this adapter.
    settings = _get().settings
    login = settings.admin_login.get_secret_value()
    if (
        not isinstance(actor, Principal)
        or not login
        or not settings.admin_password.get_secret_value()
        or not hmac.compare_digest(actor.login.encode(), login.encode())
    ):
        raise AccessDenied("Administrative access is required")
    return actor.login


def _username(value):
    return None if value is None else TypeAdapter(TelegramUsername).validate_python(value)


def _case(row):
    return SupportCase.model_validate({key: row[key] for key in SupportCase.model_fields})


async def _ticket(session, case_id, lock=False):
    statement = select(_SupportCase).where(_SupportCase.case_id == case_id)
    row = await _one(session, statement.with_for_update() if lock else statement)
    if row is None or row["state"] not in ("open", "closed"):
        raise NotFound("Ticket not found")
    return row


async def _answered_request(session, user_id, request_id=None):
    statement = select(_Request).where(
        _Request.user_id == user_id, _Request.result["kind"].as_string() == "answered"
    )
    if request_id is not None:
        statement = statement.where(_Request.request_id == request_id)
    rows = await _all(session, statement.order_by(_Request.request_id.desc()))
    for row in rows:
        deliveries = await _all(
            session,
            select(_Delivery.parts, _Delivery.receipts).where(
                _Delivery.request_id == row["request_id"], _Delivery.purpose == "answer"
            ),
        )
        for delivery in deliveries:
            parts = _data(delivery["parts"]) or []
            for receipt in _data(delivery["receipts"]):
                number = receipt["part_no"]
                if number < len(parts) and parts[number]["kind"] == "text":
                    return row
    return None


def _history(request):
    input_value = _decode(QuestionInput, request["input"])
    outcome = _decode(QuestionOutcome, request["result"])
    question_text = (
        input_value.text
        if input_value.kind == "text"
        else (request["prepared_text"] or input_value.caption or "[Изображение пользователя]")
    )
    answer_text = (
        outcome.result.answer_text
        if outcome.kind == "answered"
        else outcome.text if outcome.kind == "notice" else None
    )
    return [
        HistoryEntry(
            request_id=request["request_id"],
            question_text=question_text,
            answer_text=answer_text,
            outcome=outcome.kind,
            received_at=request["received_at"],
        )
    ]
