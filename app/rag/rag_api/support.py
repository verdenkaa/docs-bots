"""Contracts: docs/documents/telegram/module/service-entrypoints.md.

Support и административные операции через Runtime.
"""


from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID

from rag.rag_api.config import get_settings
from rag.runtime.postgresql import support as repository
from rag.rag_api._observability import _operation, _step, _call
from rag.contracts import StorageError

from rag.rag_api.contracts import IntakeResult, MetricsSnapshot, Principal, SupportCase, SupportCommand, TicketMessage, TicketView


async def handle_support_action(command: SupportCommand) -> IntakeResult:
    """Идемпотентно применить реакцию, начало ожидания или подтверждение помощи.

    Args:
        command: Пользователь, update и нормализованное действие.

    Returns:
        IntakeResult.
    """
    with _operation("support_action", update_id=command.update_id, action=command.action.kind) as trace:
        with _step("validate_support_command", error_code="support_command_invalid"):
            command = SupportCommand.model_validate(command.model_dump())
        check_at = command.received_at + timedelta(minutes=10) if command.action.kind == "start" else None
        fields = {"action": command.action.kind, "update_id": command.update_id}
        if command.action.request_id is not None:
            fields["request_id"] = command.action.request_id
        if command.action.case_id is not None:
            fields["case_id"] = str(command.action.case_id)
        try:
            with _step("record_support_action", error_code="support_storage_error", **fields) as step:
                result = await repository.record_support_action(command, check_at)
                step.update(outcome=result.kind, deduplication="runtime_ledger")
                if result.case_id:
                    step["case_id"] = str(result.case_id)
                if result.request_id:
                    step["request_id"] = result.request_id
                if check_at:
                    step["requested_check_at"] = check_at.isoformat()
            trace.fields.update(outcome=result.kind, request_id=result.request_id,
                                case_id=str(result.case_id) if result.case_id else None)
            return result
        except StorageError:
            trace.fields.update(outcome="retry_later", error_code="support_storage_error")
            return IntakeResult(kind="retry_later")


async def run_support_batch(limit: int) -> int:
    """Продолжить сохранённые ожидания и создание тикетов без сброса таймеров.

    Args:
        limit: Предел обработки готовых обращений.

    Returns:
        Целое число.
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be positive")
    with _operation("support_batch") as trace:
        now = datetime.now(timezone.utc)
        count = 0
        pending = await _call("list_pending_support", repository.list_pending_support, now, limit)
        for case in pending:
            fields = {"case_id": str(case.case_id), "request_id": case.request_id,
                      "previous_state": case.state}
            if case.state == "waiting":
                changed = await _call("queue_support_confirmation", repository.queue_support_confirmation,
                                      case.case_id, now, _fields=fields)
                count += int(changed)
            elif case.state == "creating":
                with _step("create_support_ticket", error_code="support_ticket_failed", **fields) as step:
                    opened = await repository.create_support_ticket(
                        case.case_id, get_settings().public_url + "/tickets/" + str(case.case_id))
                    step["outcome"] = opened.state
                count += int(opened.state == "open")
        trace.fields.update(outcome="completed", transitions=count)
        return count


async def list_tickets(actor: Principal, limit: int=50) -> list[SupportCase]:
    """Получить список тикетов админ-панели с ограничением числа записей.

    Args:
        actor: Проверенный администратор.
        limit: Предел выдачи.

    Returns:
        Список SupportCase.
    """
    with _operation("list_tickets"):
        return await _call("read_ticket_list", repository.read_ticket_list, actor, limit)


async def get_ticket(case_id: UUID, limit: int=50, before_message_id: int | None=None) -> TicketView:
    """Получить доступный тикет, исходный контекст и дальнейшие сообщения.

    Args:
        case_id: Идентификатор из URL.
        limit: Размер страницы сообщений (1..100).
        before_message_id: Исключающий курсор предыдущей страницы.

    Returns:
        TicketView.
    """
    with _operation("get_ticket", case_id=str(case_id)) as trace:
        result = await _call("read_ticket", repository.read_ticket, case_id, limit, before_message_id)
        trace.fields["outcome"] = result.case.state
        return result


async def add_ticket_message(actor: Principal | None, case_id: UUID, client_message_id: UUID, text: str) -> TicketMessage:
    """Добавить реплику с ключом повтора в доступный открытый тикет.

    Args:
        actor: None для публичного посетителя; проверенный Principal для оператора.
        case_id: UUID открытого тикета из ссылки.
        client_message_id: Ключ повтора одного сообщения.
        text: Текст реплики, не исполняется как HTML.

    Returns:
        TicketMessage.
    """
    with _operation("add_ticket_message", case_id=str(case_id)) as trace:
        result = await _call("append_ticket_message", repository.append_ticket_message,
                             actor, case_id, client_message_id, text)
        trace.fields.update(outcome="saved", ticket_message_id=result.message_id)
        return result


async def close_ticket(actor: Principal, case_id: UUID) -> SupportCase:
    """Идемпотентно закрыть тикет с проверкой полномочий администратора.

    Args:
        actor: Проверенный оператор и тикет.
        case_id: Проверенный оператор и тикет.

    Returns:
        SupportCase.
    """
    with _operation("close_ticket", case_id=str(case_id)):
        return await _call("close_ticket_record", repository.close_ticket_record, actor, case_id)


async def get_metrics(actor: Principal, from_at: datetime, to_at: datetime) -> MetricsSnapshot:
    """Прочитать наблюдаемые счётчики и длительности за период.

    Args:
        actor: Проверенный admin.
        from_at: Полуоткрытый период, from_at &lt; to_at.
        to_at: Полуоткрытый период, from_at &lt; to_at.

    Returns:
        MetricsSnapshot.
    """
    with _operation("get_metrics"):
        return await _call("read_metrics", repository.read_metrics, actor, from_at, to_at)
