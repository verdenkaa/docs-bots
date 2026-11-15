"""Bind a normalized support action to a saved button and Telegram receipt."""

from ._db import _all, _data
from sqlalchemy import or_, select
from ._models import _Delivery


def _callback_data(action):
    if action.kind == "offer":
        return ("a:" if action.reason == "dissatisfied" else "h:") + str(action.request_id)
    if action.kind == "start":
        return "s:" + str(action.request_id)
    return ("y:" if action.kind == "confirm" else "n:") + str(action.case_id)


async def _callback_state(session, command):
    action = command.action
    encoded = _callback_data(action)
    targets = []
    if action.request_id is not None:
        targets.append(_Delivery.request_id == action.request_id)
    if action.case_id is not None:
        targets.append(_Delivery.case_id == action.case_id)
    if not targets:
        return "ignored"
    rows = await _all(
        session,
        select(_Delivery).where(
            _Delivery.user_id == command.user_id,
            _Delivery.chat_id == command.chat_id,
            or_(*targets),
        ),
    )
    sending = False
    for row in rows:
        if (action.request_id is not None and action.request_id != row["request_id"]) or (
            action.case_id is not None and action.case_id != row["case_id"]
        ):
            continue
        receipts = {receipt["part_no"]: receipt for receipt in _data(row["receipts"])}
        for number, part in enumerate(_data(row["parts"]) or []):
            if part["kind"] != "text" or not any(
                button.get("callback_data") == encoded for button in part["actions"]
            ):
                continue
            receipt = receipts.get(number)
            if receipt is not None and receipt["message_id"] == command.callback_message_id:
                return "valid"
            if receipt is None and row["state"] == "sending":
                sending = True
    return "retry_later" if sending else "ignored"
