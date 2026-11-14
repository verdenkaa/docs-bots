"""Authorization and snapshots shared by support state transitions."""

from rag.contracts import QuestionInput, QuestionOutcome
from rag.rag_api.contracts import AccessDenied, HistoryEntry, NotFound, SupportCase
from ._db import _lock, _one, _all, _decode, _data


async def _actor(connection, actor, roles=("employee", "operator", "admin")):
    await _lock(connection, "user", actor.user_id)
    row = await _one(connection, "SELECT * FROM employees WHERE user_id=:id", id=actor.user_id)
    if row is None or not row["enabled"] or row["role"] not in roles:
        raise AccessDenied("Current access does not allow this operation")
    return row["role"]


def _case(row):
    return SupportCase.model_validate({key: row[key] for key in SupportCase.model_fields})


async def _ticket(connection, actor, case_id, role, lock=False):
    row = await _one(connection, "SELECT * FROM support_cases WHERE case_id=:id" +
                     (" FOR UPDATE" if lock else ""), id=case_id)
    if row is None or row["state"] not in ("open", "closed") or (role == "employee" and row["user_id"] != actor.user_id):
        raise NotFound("Ticket not found")
    return row


async def _answered_request(connection, user_id, request_id=None):
    rows = await _all(connection, """SELECT r.* FROM rag_requests r
        WHERE user_id=:user AND result->>'kind'='answered'
        AND (CAST(:request AS bigint) IS NULL OR request_id=:request) ORDER BY request_id DESC""",
        user=user_id, request=request_id)
    for row in rows:
        deliveries = await _all(connection, "SELECT parts,receipts FROM rag_deliveries WHERE request_id=:id AND purpose='answer'",
                                id=row["request_id"])
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
    question_text = (input_value.text if input_value.kind == "text" else
                     (request["prepared_text"] or input_value.caption or "[Изображение пользователя]"))
    answer_text = (outcome.result.answer_text if outcome.kind == "answered" else
                   outcome.text if outcome.kind == "notice" else None)
    return [HistoryEntry(request_id=request["request_id"],question_text=question_text,
                         answer_text=answer_text,outcome=outcome.kind,received_at=request["received_at"])]
