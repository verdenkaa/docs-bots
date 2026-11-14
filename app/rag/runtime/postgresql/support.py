"""Contracts: docs/documents/infrastructure/postgresql/support.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from datetime import datetime
from uuid import UUID

from rag.rag_api.contracts import AccessRecord, IntakeResult, MetricsSnapshot, Principal, SupportCase, SupportCommand, TicketMessage, TicketView


async def read_user_access(user_id: int) -> AccessRecord | None:
    """Прочитать текущую запись допуска, включая отключённого сотрудника.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        user_id: ID отправителя.

    Returns:
        AccessRecord или None.
    """
    raise NotImplementedError


async def write_user_access(actor: Principal, record: AccessRecord) -> AccessRecord:
    """Изменить допуск под проверкой администратора и синхронизацией приёма вопросов.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенный admin.
        record: Новое состояние сотрудника.

    Returns:
        AccessRecord.
    """
    raise NotImplementedError


async def record_support_action(command: SupportCommand, check_at: datetime | None) -> IntakeResult:
    """Атомарно проверить управляющий update, владельца и стадию обращения.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        command: Update, сотрудник и действие.
        check_at: Для start задаётся RAG API как received_at+10 минут, иначе None.

    Returns:
        IntakeResult.
    """
    raise NotImplementedError


async def list_pending_support(now: datetime, limit: int) -> list[SupportCase]:
    """Подобрать наступившие ожидания и незавершённое создание тикетов.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        now: Готовность ожиданий.
        limit: Предел пачки.

    Returns:
        Список SupportCase.
    """
    raise NotImplementedError


async def queue_support_confirmation(case_id: UUID, now: datetime) -> bool:
    """Перевести ручное ожидание в запрос подтверждения с самостоятельной доставкой.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        case_id: Истёкшее ожидание.
        now: Проверка наступления срока.

    Returns:
        bool.
    """
    raise NotImplementedError


async def create_support_ticket(case_id: UUID, ticket_url: str) -> SupportCase:
    """Идемпотентно открыть локальный тикет и создать доставку реальной ссылки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        case_id: Обращение в creating.
        ticket_url: Адрес из конфигурации RAG API + case_id.

    Returns:
        SupportCase.
    """
    raise NotImplementedError


async def read_ticket_list(actor: Principal, limit: int) -> list[SupportCase]:
    """Прочитать доступные пользователю тикеты, применив права до лимита.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенный сотрудник/оператор.
        limit: Предел выдачи.

    Returns:
        Список SupportCase.
    """
    raise NotImplementedError


async def read_ticket(actor: Principal, case_id: UUID) -> TicketView:
    """Прочитать разрешённый тикет, контекст и сообщения.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенный пользователь.
        case_id: Тикет.

    Returns:
        TicketView.
    """
    raise NotImplementedError


async def append_ticket_message(actor: Principal, case_id: UUID, client_message_id: UUID, text: str) -> TicketMessage:
    """Атомарно добавить реплику с ключом повтора в открытый доступный тикет.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Автор.
        case_id: Открытый тикет.
        client_message_id: Ключ повтора реплики.
        text: Содержимое, хранится как текст.

    Returns:
        TicketMessage.
    """
    raise NotImplementedError


async def close_ticket_record(actor: Principal, case_id: UUID) -> SupportCase:
    """Зафиксировать закрытие тикета с проверкой текущих прав.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Operator/admin.
        case_id: Тикет.

    Returns:
        SupportCase.
    """
    raise NotImplementedError


async def read_metrics(actor: Principal, from_at: datetime, to_at: datetime) -> MetricsSnapshot:
    """Прочитать наблюдаемые счётчики и длительности за полуоткрытый период.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Admin.
        from_at: Начало включительно.
        to_at: Конец исключительно.

    Returns:
        MetricsSnapshot.
    """
    raise NotImplementedError
