"""Контрактные пути ручной поддержки и web-операций без подключения к БД."""

from datetime import timedelta
import unittest
from uuid import uuid4

from contract_examples import NOW, returned, validate_call_arguments
from rag.rag_api.access import check_access, set_user_access
from rag.rag_api.contracts import (
    AccessRecord, HistoryEntry, IntakeResult, Principal, SupportAction, SupportCase,
    SupportCommand, TicketMessage, TicketView,
)
from rag.rag_api.support import add_ticket_message, get_ticket, handle_support_action, run_support_batch
from rag.runtime.postgresql import support as pg


class SupportContractTests(unittest.TestCase):
    def test_manual_request_to_open_ticket_and_message(self):
        admin = Principal(user_id=99, role="admin")
        employee = Principal(user_id=7, role="employee")
        access = AccessRecord(user_id=employee.user_id, enabled=True, role=employee.role)
        validate_call_arguments(set_user_access, admin, access)
        validate_call_arguments(pg.write_user_access, admin, access)
        validate_call_arguments(check_access, employee.user_id)

        command = SupportCommand(
            update_id=20, user_id=7, chat_id=7, received_at=NOW,
            action=SupportAction(kind="start", request_id=1),
        )
        validate_call_arguments(handle_support_action, command)
        check_at = command.received_at + timedelta(minutes=10)
        validate_call_arguments(pg.record_support_action, command, check_at)
        case_id = uuid4()
        returned(handle_support_action, IntakeResult(kind="handled", request_id=1, case_id=case_id))
        waiting = SupportCase(case_id=case_id, request_id=1, user_id=7, state="waiting",
                              reason="human_request", check_at=check_at)
        validate_call_arguments(run_support_batch, 20)
        validate_call_arguments(pg.queue_support_confirmation, waiting.case_id, check_at)
        confirmation = SupportCommand(
            update_id=21, user_id=7, chat_id=7, received_at=check_at,
            action=SupportAction(kind="confirm", case_id=waiting.case_id),
        )
        validate_call_arguments(pg.record_support_action, confirmation, None)
        ticket_url = f"https://bot.example/tickets/{waiting.case_id}"
        validate_call_arguments(pg.create_support_ticket, waiting.case_id, ticket_url)
        ticket = returned(pg.create_support_ticket, SupportCase(
            case_id=waiting.case_id, request_id=1, user_id=7, state="open",
            reason="human_request", check_at=check_at, ticket_url=ticket_url, created_at=check_at))
        validate_call_arguments(get_ticket, employee, ticket.case_id)
        history = HistoryEntry(request_id=1, question_text="Где фильтр?", answer_text="Откройте меню",
                               outcome="answered", received_at=NOW)
        view = returned(get_ticket, TicketView(case=ticket, history=[history], messages=[]))
        key = uuid4()
        validate_call_arguments(add_ticket_message, employee, view.case.case_id, key, "Не нашёл")
        validate_call_arguments(pg.append_ticket_message, employee, view.case.case_id, key, "Не нашёл")
        message = returned(add_ticket_message, TicketMessage(
            message_id=1, case_id=view.case.case_id, author_id=employee.user_id,
            author_role=employee.role, text="Не нашёл", created_at=check_at))
        self.assertEqual(message.case_id, ticket.case_id)


if __name__ == "__main__":
    unittest.main()
