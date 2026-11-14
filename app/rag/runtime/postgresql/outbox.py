"""Contracts: docs/documents/infrastructure/postgresql/outbox.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from uuid import UUID

from pydantic import AwareDatetime, Field

from rag.dto import DTO
from rag.runtime.rabbitmq import OutboxNotice, WorkRoute


class ReadyOutboxRow(DTO):
    """Внутренняя запись для Relay: сообщение и время готовности для диагностики.

    available_at не требуется добавлять в сообщение RabbitMQ.
    """
    notice: OutboxNotice = Field(description='Уведомление, передаваемое Relay в publish_notice.')
    available_at: AwareDatetime = Field(description='Сохранённое время готовности outbox в UTC для расчёта ожидания публикации.')


async def list_ready_outbox(limit: int) -> list[ReadyOutboxRow]:
    """Прочитать неопубликованные уведомления, для которых наступил available_at.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список объектов Pydantic `ReadyOutboxRow`. `[]` допустим.
    """
    raise NotImplementedError


async def mark_outbox_published(message_id: UUID) -> bool:
    """Отметить публикацию после подтверждения маршрутизации брокером.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        message_id: ID из опубликованного OutboxNotice.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError


async def recover_pending_work(route: WorkRoute, limit: int) -> int:
    """Добавить outbox для готовой незавершённой работы с учётом окна доставки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        route: Какой этап восстанавливаем.
        limit: Положительный предел размера пачки.

    Returns:
        Одно целое число.
    """
    raise NotImplementedError
