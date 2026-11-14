"""Contracts: docs/documents/infrastructure/postgresql/support.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from rag.rag_api.contracts import (
    AccessRecord,
    IntakeResult,
    MetricsSnapshot,
    Principal,
    SupportCase,
    SupportCommand,
    TicketMessage,
    TicketView,
)

from datetime import datetime as _DateTime
from uuid import uuid4
from rag.rag_api.contracts import Conflict, NotFound, HistoryEntry
from rag.contracts import RagResult
from rag.runtime._validation import _positive, _text, _utc
from ._db import (
    _transaction,
    _one,
    _all,
    _execute,
    _lock,
    _delivery,
    _json,
    _decode,
    _data,
    _prior_update,
    _save_update,
)
from ._support import _actor, _case, _ticket, _answered_request, _history, _username
from ._callbacks import _callback_state
from ._models import _Employee

from sqlalchemy import and_, func, insert, or_, select, update
from ._models import _Delivery, _Request, _SupportCase, _SupportMessage, _TelegramUpdate


async def read_user_access(username: str | None) -> AccessRecord | None:
    """Прочитать текущую запись допуска, включая отключённого сотрудника.

    Args:
        username: Нормализованный username или None.

    Returns:
        AccessRecord или None.
    """
    username = _username(username)
    if username is None:
        return None
    async with _transaction() as session:
        row = await _one(session, select(_Employee).where(_Employee.username == username))
        return AccessRecord(**row) if row else None


async def write_user_access(actor: Principal, record: AccessRecord) -> AccessRecord:
    """Изменить допуск под проверкой администратора и синхронизацией приёма вопросов.

    Args:
        actor: Проверенный admin.
        record: Новое состояние сотрудника.

    Returns:
        AccessRecord.
    """
    record = AccessRecord.model_validate_json(record.model_dump_json())
    async with _transaction() as session:
        await _actor(session, actor)
        await _lock(session, "username", record.username)
        employee = await session.get(_Employee, record.username)
        if employee is None:
            session.add(_Employee(username=record.username, enabled=record.enabled))
        else:
            employee.enabled = record.enabled
        return record


async def record_support_action(command: SupportCommand, check_at: datetime | None) -> IntakeResult:
    """Атомарно проверить управляющий update, владельца и стадию обращения.

    Args:
        command: Update, сотрудник и действие.
        check_at: Для start задаётся RAG API как received_at+10 минут, иначе None.

    Returns:
        IntakeResult.
    """
    command = SupportCommand.model_validate_json(command.model_dump_json())
    if check_at is not None:
        _utc(check_at)
    async with _transaction() as session:
        previous = await _prior_update(session, command.update_id)
        if previous is not None:
            return previous
        if command.username is not None:
            await _lock(session, "username", command.username)
        access = await _one(
            session, select(_Employee.enabled).where(_Employee.username == command.username)
        )
        await _lock(session, "user", command.user_id)
        action = command.action
        normalized = action.model_dump(mode="json")
        normalized.update(origin=command.origin, callback_message_id=command.callback_message_id)
        result = IntakeResult(kind="ignored")
        if access is None or not access["enabled"]:
            delivery = await _delivery(
                session,
                user_id=command.user_id,
                chat_id=command.chat_id,
                purpose="denied",
                key="update:" + str(command.update_id),
            )
            result = IntakeResult(kind="denied", delivery_id=delivery)
        elif (
            command.origin == "callback"
            and (callback_state := await _callback_state(session, command)) != "valid"
        ):
            if callback_state == "retry_later":
                return IntakeResult(kind="retry_later")
            # Unknown or stale buttons get a durable ignored ledger entry below.
        elif action.kind == "offer":
            request = await _answered_request(session, command.user_id, action.request_id)
            if request is None:
                delivery = await _delivery(
                    session,
                    user_id=command.user_id,
                    chat_id=command.chat_id,
                    purpose="first_question",
                    key="update:" + str(command.update_id),
                )
                result = IntakeResult(kind="handled", delivery_id=delivery)
            else:
                normalized["request_id"] = request["request_id"]
                case = await _one(
                    session,
                    select(_SupportCase).where(_SupportCase.request_id == request["request_id"]),
                )
                if case is None:
                    delivery = await _delivery(
                        session,
                        user_id=command.user_id,
                        chat_id=command.chat_id,
                        request_id=request["request_id"],
                        purpose="support_offer",
                        key="offer:" + str(request["request_id"]),
                    )
                    result = IntakeResult(
                        kind="handled", request_id=request["request_id"], delivery_id=delivery
                    )
                else:
                    result = IntakeResult(
                        kind="handled", request_id=request["request_id"], case_id=case["case_id"]
                    )
        elif action.kind == "start":
            request = await _answered_request(session, command.user_id, action.request_id)
            offer = await _one(
                session,
                select(_TelegramUpdate.action)
                .where(
                    _TelegramUpdate.user_id == command.user_id,
                    _TelegramUpdate.action["kind"].as_string() == "offer",
                    _TelegramUpdate.action["request_id"].as_string() == str(action.request_id),
                    _TelegramUpdate.result["kind"].as_string() == "handled",
                )
                .order_by(_TelegramUpdate.received_at.desc())
                .limit(1),
            )
            if request is not None and offer is not None:
                if check_at is None:
                    raise ValueError("Starting support requires check_at")
                case = await _one(
                    session,
                    select(_SupportCase)
                    .where(_SupportCase.request_id == action.request_id)
                    .with_for_update(),
                )
                if case is None:
                    case = await _one(
                        session,
                        insert(_SupportCase)
                        .values(
                            case_id=uuid4(),
                            request_id=action.request_id,
                            user_id=command.user_id,
                            state="waiting",
                            reason=_data(offer["action"])["reason"],
                            check_at=check_at,
                        )
                        .returning(*_SupportCase.__table__.c),
                    )
                    await _delivery(
                        session,
                        user_id=command.user_id,
                        chat_id=command.chat_id,
                        request_id=request["request_id"],
                        case_id=case["case_id"],
                        purpose="notice",
                        content=RagResult(
                            answer_text="Вы можете поискать ответ самостоятельно. Через 10 минут уточню, нужна ли помощь.",
                            sources=[],
                        ),
                        key="support-wait:" + str(case["case_id"]),
                    )
                result = IntakeResult(
                    kind="handled", request_id=case["request_id"], case_id=case["case_id"]
                )
        else:
            case = await _one(
                session,
                select(_SupportCase)
                .where(_SupportCase.case_id == action.case_id)
                .with_for_update(),
            )
            if (
                case is not None
                and case["user_id"] == command.user_id
                and case["state"] == "awaiting_confirmation"
                and (action.request_id is None or action.request_id == case["request_id"])
            ):
                state = "creating" if action.kind == "confirm" else "cancelled"
                await _execute(
                    session,
                    update(_SupportCase)
                    .where(_SupportCase.case_id == action.case_id)
                    .values(state=state),
                )
                result = IntakeResult(
                    kind="handled", request_id=case["request_id"], case_id=case["case_id"]
                )
        await _save_update(
            session,
            command.update_id,
            command.user_id,
            command.chat_id,
            command.received_at,
            result,
            normalized,
        )
        return result


async def list_pending_support(now: datetime, limit: int) -> list[SupportCase]:
    """Подобрать наступившие ожидания и незавершённое создание тикетов.

    Args:
        now: Готовность ожиданий.
        limit: Предел пачки.

    Returns:
        Список SupportCase.
    """
    _utc(now)
    _positive(limit)
    async with _transaction() as session:
        rows = await _all(
            session,
            select(_SupportCase)
            .where(
                or_(
                    _SupportCase.state == "creating",
                    and_(_SupportCase.state == "waiting", _SupportCase.check_at <= now),
                )
            )
            .order_by(_SupportCase.check_at.nulls_first(), _SupportCase.case_id)
            .limit(limit),
        )
        return [_case(row) for row in rows]


async def queue_support_confirmation(case_id: UUID, now: datetime) -> bool:
    """Перевести ручное ожидание в запрос подтверждения с самостоятельной доставкой.

    Args:
        case_id: Истёкшее ожидание.
        now: Проверка наступления срока.

    Returns:
        bool.
    """
    _utc(now)
    async with _transaction() as session:
        initial = await _one(
            session, select(_SupportCase.user_id).where(_SupportCase.case_id == case_id)
        )
        if initial is None:
            return False
        await _lock(session, "user", initial["user_id"])
        row = await _one(
            session, select(_SupportCase).where(_SupportCase.case_id == case_id).with_for_update()
        )
        if row["state"] != "waiting" or row["check_at"] > now:
            return False
        await _execute(
            session,
            update(_SupportCase)
            .where(_SupportCase.case_id == case_id)
            .values(state="awaiting_confirmation"),
        )
        request = await _one(
            session, select(_Request.chat_id).where(_Request.request_id == row["request_id"])
        )
        await _delivery(
            session,
            user_id=row["user_id"],
            chat_id=request["chat_id"],
            request_id=row["request_id"],
            case_id=case_id,
            purpose="support_confirmation",
            key="support-confirm:" + str(case_id),
        )
        return True


async def create_support_ticket(case_id: UUID, ticket_url: str) -> SupportCase:
    """Идемпотентно открыть локальный тикет и создать доставку реальной ссылки.

    Args:
        case_id: Обращение в creating.
        ticket_url: Адрес из конфигурации RAG API + case_id.

    Returns:
        SupportCase.
    """
    _text(ticket_url)
    async with _transaction() as session:
        initial = await _one(
            session, select(_SupportCase.user_id).where(_SupportCase.case_id == case_id)
        )
        if initial is None:
            raise NotFound("Support case not found")
        await _lock(session, "user", initial["user_id"])
        row = await _one(
            session, select(_SupportCase).where(_SupportCase.case_id == case_id).with_for_update()
        )
        if row["state"] in ("open", "closed"):
            return _case(row)
        if row["state"] != "creating":
            raise Conflict("Support case is not ready for ticket creation")
        request = await _one(
            session, select(_Request).where(_Request.request_id == row["request_id"])
        )
        history = _history(request)
        row = await _one(
            session,
            update(_SupportCase)
            .where(_SupportCase.case_id == case_id)
            .values(
                state="open",
                created_at=func.clock_timestamp(),
                ticket_url=ticket_url,
                history=_data(_json([item.model_dump(mode="json") for item in history])),
            )
            .returning(*_SupportCase.__table__.c),
        )
        automatic = request["state"] == "awaiting_support"
        await _delivery(
            session,
            user_id=row["user_id"],
            chat_id=request["chat_id"],
            request_id=row["request_id"],
            case_id=case_id,
            purpose="ticket_link",
            terminal=automatic,
            key="terminal:" + str(row["request_id"]) if automatic else "ticket:" + str(case_id),
        )
        if automatic:
            await _execute(
                session,
                update(_Request)
                .where(_Request.request_id == row["request_id"])
                .values(state="awaiting_delivery"),
            )
        return _case(row)


async def read_ticket_list(actor: Principal, limit: int) -> list[SupportCase]:
    """Прочитать тикеты для проверенного администратора.

    Args:
        actor: Проверенный администратор.
        limit: Предел выдачи.

    Returns:
        Список SupportCase.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("Ticket limit must be 1..100")
    async with _transaction() as session:
        await _actor(session, actor)
        rows = await _all(
            session,
            select(_SupportCase)
            .where(_SupportCase.state.in_(("open", "closed")))
            .order_by(_SupportCase.request_id.desc())
            .limit(limit),
        )
        return [_case(row) for row in rows]


async def read_ticket(case_id: UUID) -> TicketView:
    """Прочитать разрешённый тикет, контекст и сообщения.

    Args:
        case_id: Тикет.

    Returns:
        TicketView.
    """
    async with _transaction() as session:
        row = await _ticket(session, case_id)
        messages = await _all(
            session,
            select(
                _SupportMessage.message_id,
                _SupportMessage.case_id,
                _SupportMessage.author_login,
                _SupportMessage.author_role,
                _SupportMessage.text,
                _SupportMessage.created_at,
            )
            .where(_SupportMessage.case_id == case_id)
            .order_by(_SupportMessage.message_id),
        )
        return TicketView(
            case=_case(row),
            history=_decode(list[HistoryEntry], row["history"]),
            messages=[TicketMessage(**item) for item in messages],
        )


async def append_ticket_message(
    actor: Principal | None, case_id: UUID, client_message_id: UUID, text: str
) -> TicketMessage:
    """Атомарно добавить реплику с ключом повтора в открытый доступный тикет.

    Args:
        actor: None для посетителя либо проверенный администратор.
        case_id: Открытый тикет.
        client_message_id: Ключ повтора реплики.
        text: Содержимое, хранится как текст.

    Returns:
        TicketMessage.
    """
    _text(text)
    async with _transaction() as session:
        login = await _actor(session, actor) if actor is not None else None
        role = "operator" if actor is not None else "visitor"
        case = await _ticket(session, case_id, lock=True)
        old = await _one(
            session,
            select(
                _SupportMessage.message_id,
                _SupportMessage.case_id,
                _SupportMessage.author_login,
                _SupportMessage.author_role,
                _SupportMessage.text,
                _SupportMessage.created_at,
            ).where(
                _SupportMessage.case_id == case_id,
                _SupportMessage.author_role == role,
                _SupportMessage.client_message_id == client_message_id,
            ),
        )
        if old is not None:
            if old["text"] != text:
                raise Conflict("Message id reused with different text")
            return TicketMessage(**old)
        if case["state"] != "open":
            raise Conflict("Ticket is closed")
        row = await _one(
            session,
            insert(_SupportMessage)
            .values(
                case_id=case_id,
                author_login=login,
                author_role=role,
                client_message_id=client_message_id,
                text=text,
            )
            .returning(
                _SupportMessage.message_id,
                _SupportMessage.case_id,
                _SupportMessage.author_login,
                _SupportMessage.author_role,
                _SupportMessage.text,
                _SupportMessage.created_at,
            ),
        )
        return TicketMessage(**row)


async def close_ticket_record(actor: Principal, case_id: UUID) -> SupportCase:
    """Зафиксировать закрытие тикета с проверкой текущих прав.

    Args:
        actor: Проверенный администратор.
        case_id: Тикет.

    Returns:
        SupportCase.
    """
    async with _transaction() as session:
        await _actor(session, actor)
        row = await _ticket(session, case_id, lock=True)
        if row["state"] == "open":
            row = await _one(
                session,
                update(_SupportCase)
                .where(_SupportCase.case_id == case_id)
                .values(state="closed", closed_at=func.clock_timestamp())
                .returning(*_SupportCase.__table__.c),
            )
        return _case(row)


async def read_metrics(actor: Principal, from_at: datetime, to_at: datetime) -> MetricsSnapshot:
    """Прочитать наблюдаемые счётчики и длительности за полуоткрытый период.

    Args:
        actor: Admin.
        from_at: Начало включительно.
        to_at: Конец исключительно.

    Returns:
        MetricsSnapshot.
    """
    _utc(from_at)
    _utc(to_at)
    if from_at >= to_at:
        raise ValueError("Invalid metrics period")
    async with _transaction() as session:
        await _actor(session, actor)
        requests = await _all(
            session,
            select(_Request).where(_Request.received_at >= from_at, _Request.received_at < to_at),
        )
        answered = escalated = dissatisfied = 0
        replies, tickets = [], []
        for request in requests:
            outcome = _data(request["result"])
            answered += int(outcome is not None and outcome["kind"] == "answered")
            case = await _one(
                session,
                select(_SupportCase).where(_SupportCase.request_id == request["request_id"]),
            )
            escalated += int(
                (outcome is not None and outcome["kind"] == "support")
                or (case is not None and case["state"] in ("creating", "open", "closed"))
            )
            reaction = await _one(
                session,
                select(_TelegramUpdate.update_id)
                .where(
                    _TelegramUpdate.action["kind"].as_string() == "offer",
                    _TelegramUpdate.action["reason"].as_string() == "dissatisfied",
                    _TelegramUpdate.action["request_id"].as_string() == str(request["request_id"]),
                    _TelegramUpdate.result["kind"].as_string() == "handled",
                )
                .limit(1),
            )
            dissatisfied += int(reaction is not None)
            deliveries = await _all(
                session,
                select(_Delivery.parts, _Delivery.receipts).where(
                    _Delivery.request_id == request["request_id"], _Delivery.purpose == "answer"
                ),
            )
            dates = []
            for delivery in deliveries:
                parts = _data(delivery["parts"]) or []
                for receipt in _data(delivery["receipts"]):
                    if (
                        receipt["variant"] == "normal"
                        and receipt["part_no"] < len(parts)
                        and parts[receipt["part_no"]]["kind"] == "text"
                    ):
                        dates.append(_DateTime.fromisoformat(receipt["confirmed_at"]))
            if dates:
                replies.append(
                    max(0, int((min(dates) - request["received_at"]).total_seconds() * 1000))
                )
            if case is not None and case["closed_at"] is not None:
                tickets.append(
                    max(0, int((case["closed_at"] - request["received_at"]).total_seconds() * 1000))
                )
        return MetricsSnapshot(
            from_at=from_at,
            to_at=to_at,
            requests=len(requests),
            answered=answered,
            escalated=escalated,
            dissatisfied=dissatisfied,
            reply_duration_ms=replies,
            ticket_duration_ms=tickets,
            topics=None,
        )
