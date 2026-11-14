"""Contracts: docs/documents/infrastructure/postgresql/support.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from datetime import datetime
from uuid import UUID

from rag.rag_api.contracts import AccessRecord, IntakeResult, MetricsSnapshot, Principal, SupportCase, SupportCommand, TicketMessage, TicketView

from datetime import datetime as _DateTime
from uuid import uuid4
from rag.rag_api.contracts import Conflict, NotFound, HistoryEntry
from rag.contracts import RagResult
from rag.runtime._validation import _positive, _text, _utc
from ._db import (_transaction,_one,_all,_execute,_lock,_delivery,_json,_decode,_data,
                  _prior_update,_save_update)
from ._support import _actor, _case, _ticket, _answered_request, _history


async def read_user_access(username: str | None) -> AccessRecord | None:
    """Прочитать текущую запись допуска, включая отключённого сотрудника.

    Args:
        username: Нормализованный username или None.

    Returns:
        AccessRecord или None.
    """
    _positive(user_id)
    async with _transaction() as connection:
        row = await _one(connection,"SELECT * FROM employees WHERE user_id=:id",id=user_id)
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
    async with _transaction() as connection:
        for user in sorted({actor.user_id,record.user_id}):
            await _lock(connection,"user",user)
        await _actor(connection,actor,("admin",))
        await _execute(connection,"""INSERT INTO employees(user_id,enabled,role) VALUES(:id,:enabled,:role)
            ON CONFLICT(user_id) DO UPDATE SET enabled=EXCLUDED.enabled,role=EXCLUDED.role""",
            id=record.user_id,enabled=record.enabled,role=record.role)
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
    async with _transaction() as connection:
        previous = await _prior_update(connection,command.update_id)
        if previous is not None:
            return previous
        await _lock(connection,"user",command.user_id)
        access = await _one(connection,"SELECT enabled FROM employees WHERE user_id=:id",id=command.user_id)
        action = command.action
        normalized = action.model_dump(mode="json")
        result = IntakeResult(kind="ignored")
        if access is None or not access["enabled"]:
            delivery = await _delivery(connection,user_id=command.user_id,chat_id=command.chat_id,
                purpose="denied",key="update:"+str(command.update_id))
            result = IntakeResult(kind="denied",delivery_id=delivery)
        elif action.kind == "offer":
            request = await _answered_request(connection,command.user_id,action.request_id)
            if request is None:
                delivery = await _delivery(connection,user_id=command.user_id,chat_id=command.chat_id,
                    purpose="first_question",key="update:"+str(command.update_id))
                result = IntakeResult(kind="handled",delivery_id=delivery)
            else:
                normalized["request_id"] = request["request_id"]
                case = await _one(connection,"SELECT * FROM support_cases WHERE request_id=:id",id=request["request_id"])
                if case is None:
                    delivery = await _delivery(connection,user_id=command.user_id,chat_id=command.chat_id,
                        request_id=request["request_id"],purpose="support_offer",key="offer:"+str(request["request_id"]))
                    result = IntakeResult(kind="handled",request_id=request["request_id"],delivery_id=delivery)
                else:
                    result = IntakeResult(kind="handled",request_id=request["request_id"],case_id=case["case_id"])
        elif action.kind == "start":
            request = await _answered_request(connection,command.user_id,action.request_id)
            offer = await _one(connection,"""SELECT action FROM telegram_updates WHERE user_id=:user
                AND action->>'kind'='offer' AND action->>'request_id'=:request
                AND result->>'kind'='handled' ORDER BY received_at DESC LIMIT 1""",
                user=command.user_id,request=str(action.request_id))
            if request is not None and offer is not None:
                if check_at is None:
                    raise ValueError("Starting support requires check_at")
                case = await _one(connection,"SELECT * FROM support_cases WHERE request_id=:id FOR UPDATE",id=action.request_id)
                if case is None:
                    case = await _one(connection,"""INSERT INTO support_cases
                        (case_id,request_id,user_id,state,reason,check_at) VALUES
                        (:case,:request,:user,'waiting',:reason,:at) RETURNING *""",
                        case=uuid4(),request=action.request_id,user=command.user_id,
                        reason=_data(offer["action"])["reason"],at=check_at)
                    await _delivery(connection,user_id=command.user_id,chat_id=command.chat_id,
                        request_id=request["request_id"],case_id=case["case_id"],purpose="notice",
                        content=RagResult(
                            answer_text="Вы можете поискать ответ самостоятельно. Через 10 минут уточню, нужна ли помощь.",sources=[]),
                        key="support-wait:"+str(case["case_id"]))
                result = IntakeResult(kind="handled",request_id=case["request_id"],case_id=case["case_id"])
        else:
            case = await _one(connection,"SELECT * FROM support_cases WHERE case_id=:id FOR UPDATE",id=action.case_id)
            if (case is not None and case["user_id"]==command.user_id and case["state"]=="awaiting_confirmation"
                and (action.request_id is None or action.request_id==case["request_id"])):
                state = "creating" if action.kind=="confirm" else "cancelled"
                await _execute(connection,"UPDATE support_cases SET state=:state WHERE case_id=:id",
                    state=state,id=action.case_id)
                result = IntakeResult(kind="handled",request_id=case["request_id"],case_id=case["case_id"])
        await _save_update(connection,command.update_id,command.user_id,command.chat_id,command.received_at,result,normalized)
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
    async with _transaction() as connection:
        rows = await _all(connection,"""SELECT * FROM support_cases WHERE state='creating'
            OR (state='waiting' AND check_at<=:now) ORDER BY check_at NULLS FIRST,case_id LIMIT :limit""",now=now,limit=limit)
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
    async with _transaction() as connection:
        initial = await _one(connection,"SELECT user_id FROM support_cases WHERE case_id=:id",id=case_id)
        if initial is None:
            return False
        await _lock(connection,"user",initial["user_id"])
        row = await _one(connection,"SELECT * FROM support_cases WHERE case_id=:id FOR UPDATE",id=case_id)
        if row["state"]!="waiting" or row["check_at"]>now:
            return False
        await _execute(connection,"UPDATE support_cases SET state='awaiting_confirmation' WHERE case_id=:id",id=case_id)
        request = await _one(connection,"SELECT chat_id FROM rag_requests WHERE request_id=:id",id=row["request_id"])
        await _delivery(connection,user_id=row["user_id"],chat_id=request["chat_id"],request_id=row["request_id"],
            case_id=case_id,purpose="support_confirmation",key="support-confirm:"+str(case_id))
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
    async with _transaction() as connection:
        initial = await _one(connection,"SELECT user_id FROM support_cases WHERE case_id=:id",id=case_id)
        if initial is None:
            raise NotFound("Support case not found")
        await _lock(connection,"user",initial["user_id"])
        row = await _one(connection,"SELECT * FROM support_cases WHERE case_id=:id FOR UPDATE",id=case_id)
        if row["state"] in ("open","closed"):
            return _case(row)
        if row["state"]!="creating":
            raise Conflict("Support case is not ready for ticket creation")
        request = await _one(connection,"SELECT * FROM rag_requests WHERE request_id=:id",id=row["request_id"])
        history = _history(request)
        row = await _one(connection,"""UPDATE support_cases SET state='open',created_at=clock_timestamp(),
            ticket_url=:url,history=CAST(:history AS jsonb) WHERE case_id=:id RETURNING *""",
            url=ticket_url,history=_json([item.model_dump(mode="json") for item in history]),id=case_id)
        automatic = request["state"]=="awaiting_support"
        await _delivery(connection,user_id=row["user_id"],chat_id=request["chat_id"],request_id=row["request_id"],
            case_id=case_id,purpose="ticket_link",terminal=automatic,
            key="terminal:"+str(row["request_id"]) if automatic else "ticket:"+str(case_id))
        if automatic:
            await _execute(connection,"UPDATE rag_requests SET state='awaiting_delivery' WHERE request_id=:id",id=row["request_id"])
        return _case(row)


async def read_ticket_list(actor: Principal, limit: int) -> list[SupportCase]:
    """Прочитать тикеты для проверенного администратора.

    Args:
        actor: Проверенный администратор.
        limit: Предел выдачи.

    Returns:
        Список SupportCase.
    """
    if type(limit) is not int or not 1<=limit<=100:
        raise ValueError("Ticket limit must be 1..100")
    async with _transaction() as connection:
        role = await _actor(connection,actor)
        rows = await _all(connection,"""SELECT * FROM support_cases WHERE state IN ('open','closed')
            AND (:all_users OR user_id=:user) ORDER BY created_at DESC,case_id LIMIT :limit""",
            all_users=role in ("operator","admin"),user=actor.user_id,limit=limit)
        return [_case(row) for row in rows]


async def read_ticket(case_id: UUID) -> TicketView:
    """Прочитать разрешённый тикет, контекст и сообщения.

    Args:
        case_id: Тикет.

    Returns:
        TicketView.
    """
    async with _transaction() as connection:
        role = await _actor(connection,actor)
        row = await _ticket(connection,actor,case_id,role)
        messages = await _all(connection,"""SELECT message_id,case_id,author_id,author_role,text,created_at
            FROM support_messages WHERE case_id=:id ORDER BY message_id""",id=case_id)
        return TicketView(case=_case(row),history=_decode(list[HistoryEntry],row["history"]),
                          messages=[TicketMessage(**item) for item in messages])


async def append_ticket_message(actor: Principal | None, case_id: UUID, client_message_id: UUID, text: str) -> TicketMessage:
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
    async with _transaction() as connection:
        role = await _actor(connection,actor)
        case = await _ticket(connection,actor,case_id,role,lock=True)
        old = await _one(connection,"""SELECT message_id,case_id,author_id,author_role,text,created_at
            FROM support_messages WHERE case_id=:case AND author_id=:user AND client_message_id=:client""",
            case=case_id,user=actor.user_id,client=client_message_id)
        if old is not None:
            if old["text"]!=text:
                raise Conflict("Message id reused with different text")
            return TicketMessage(**old)
        if case["state"]!="open":
            raise Conflict("Ticket is closed")
        row = await _one(connection,"""INSERT INTO support_messages(case_id,author_id,author_role,client_message_id,text)
            VALUES(:case,:user,:role,:client,:text) RETURNING message_id,case_id,author_id,author_role,text,created_at""",
            case=case_id,user=actor.user_id,role=role,client=client_message_id,text=text)
        return TicketMessage(**row)


async def close_ticket_record(actor: Principal, case_id: UUID) -> SupportCase:
    """Зафиксировать закрытие тикета с проверкой текущих прав.

    Args:
        actor: Проверенный администратор.
        case_id: Тикет.

    Returns:
        SupportCase.
    """
    async with _transaction() as connection:
        role = await _actor(connection,actor,("operator","admin"))
        row = await _ticket(connection,actor,case_id,role,lock=True)
        if row["state"]=="open":
            row = await _one(connection,"""UPDATE support_cases SET state='closed',closed_at=clock_timestamp()
                WHERE case_id=:id RETURNING *""",id=case_id)
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
    if from_at>=to_at:
        raise ValueError("Invalid metrics period")
    async with _transaction() as connection:
        await _actor(connection,actor,("admin",))
        requests = await _all(connection,"SELECT * FROM rag_requests WHERE received_at>=:start AND received_at<:end",
                              start=from_at,end=to_at)
        answered = escalated = dissatisfied = 0
        replies, tickets = [], []
        for request in requests:
            outcome = _data(request["result"])
            answered += int(outcome is not None and outcome["kind"]=="answered")
            case = await _one(connection,"SELECT * FROM support_cases WHERE request_id=:id",id=request["request_id"])
            escalated += int((outcome is not None and outcome["kind"]=="support") or
                             (case is not None and case["state"] in ("creating","open","closed")))
            reaction = await _one(connection,"""SELECT update_id FROM telegram_updates
                WHERE action->>'kind'='offer' AND action->>'reason'='dissatisfied'
                AND action->>'request_id'=:request AND result->>'kind'='handled' LIMIT 1""",
                request=str(request["request_id"]))
            dissatisfied += int(reaction is not None)
            deliveries = await _all(connection,"SELECT parts,receipts FROM rag_deliveries WHERE request_id=:id AND purpose='answer'",
                                    id=request["request_id"])
            dates = []
            for delivery in deliveries:
                parts = _data(delivery["parts"]) or []
                for receipt in _data(delivery["receipts"]):
                    if receipt["variant"]=="normal" and receipt["part_no"]<len(parts) and parts[receipt["part_no"]]["kind"]=="text":
                        dates.append(_DateTime.fromisoformat(receipt["confirmed_at"]))
            if dates:
                replies.append(max(0,int((min(dates)-request["received_at"]).total_seconds()*1000)))
            if case is not None and case["closed_at"] is not None:
                tickets.append(max(0,int((case["closed_at"]-request["received_at"]).total_seconds()*1000)))
        return MetricsSnapshot(from_at=from_at,to_at=to_at,requests=len(requests),answered=answered,
            escalated=escalated,dissatisfied=dissatisfied,reply_duration_ms=replies,ticket_duration_ms=tickets,topics=None)
