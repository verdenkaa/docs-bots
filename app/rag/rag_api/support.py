"""Contracts: docs/documents/telegram/module/service-entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from datetime import datetime
from uuid import UUID

from rag.rag_api.contracts import IntakeResult, MetricsSnapshot, Principal, SupportCase, SupportCommand, TicketMessage, TicketView


async def handle_support_action(command: SupportCommand) -> IntakeResult:
    """Идемпотентно применить реакцию, начало ожидания или подтверждение помощи.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        command: Пользователь, update и нормализованное действие.

    Returns:
        IntakeResult.
    """
    raise NotImplementedError


async def run_support_batch(limit: int) -> int:
    """Продолжить сохранённые ожидания и создание тикетов без сброса таймеров.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        limit: Предел обработки готовых обращений.

    Returns:
        Целое число.
    """
    raise NotImplementedError


async def list_tickets(actor: Principal, limit: int=50) -> list[SupportCase]:
    """Получить доступные пользователю тикеты с ограничением числа записей.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенный пользователь/оператор.
        limit: Предел выдачи.

    Returns:
        Список SupportCase.
    """
    raise NotImplementedError


async def get_ticket(actor: Principal, case_id: UUID) -> TicketView:
    """Получить доступный тикет, исходный контекст и дальнейшие сообщения.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенная личность.
        case_id: Идентификатор из URL.

    Returns:
        TicketView.
    """
    raise NotImplementedError


async def add_ticket_message(actor: Principal, case_id: UUID, client_message_id: UUID, text: str) -> TicketMessage:
    """Добавить реплику с ключом повтора в доступный открытый тикет.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенный автор и тикет.
        case_id: Проверенный автор и тикет.
        client_message_id: Ключ повтора одного сообщения.
        text: Текст реплики, не исполняется как HTML.

    Returns:
        TicketMessage.
    """
    raise NotImplementedError


async def close_ticket(actor: Principal, case_id: UUID) -> SupportCase:
    """Идемпотентно закрыть тикет с проверкой полномочий оператора или администратора.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенный оператор и тикет.
        case_id: Проверенный оператор и тикет.

    Returns:
        SupportCase.
    """
    raise NotImplementedError


async def get_metrics(actor: Principal, from_at: datetime, to_at: datetime) -> MetricsSnapshot:
    """Прочитать наблюдаемые счётчики и длительности за период.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенный admin.
        from_at: Полуоткрытый период, from_at &lt; to_at.
        to_at: Полуоткрытый период, from_at &lt; to_at.

    Returns:
        MetricsSnapshot.
    """
    raise NotImplementedError
