"""Contracts: docs/documents/infrastructure/postgresql/outbox.md."""

from pydantic import Field

from pydantic import AwareDatetime
from rag.dto import DTO


from uuid import UUID

from rag.runtime.rabbitmq import OutboxNotice, WorkRoute


class ReadyOutboxRow(DTO):
    """Внутренняя запись для Relay: сообщение и время готовности для диагностики.

    available_at не требуется добавлять в сообщение RabbitMQ.
    """

    notice: OutboxNotice = Field(description="Уведомление, передаваемое Relay в publish_notice.")
    available_at: AwareDatetime = Field(
        description="Сохранённое время готовности outbox в UTC для расчёта ожидания публикации.",
    )


async def list_ready_outbox(limit: int) -> list[ReadyOutboxRow]:
    """Прочитать неопубликованные уведомления, для которых наступил available_at.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Список ReadyOutboxRow для Relay, включая время готовности для логов.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def mark_outbox_published(message_id: UUID) -> bool:
    """Отметить публикацию после подтверждения маршрутизации брокером.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        message_id: ID из опубликованного OutboxNotice.

    Returns:
        True — отметка сохранена; False — запись отсутствует или уже отмечена.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def recover_pending_work(route: WorkRoute, limit: int) -> int:
    """Добавить outbox для готовой незавершённой работы с учётом окна доставки.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        route: Какой этап восстанавливаем.
        limit: Положительный предел размера пачки.

    Returns:
        Число новых уведомлений; не дублирует ожидающий outbox.
        Для ingestion исключает pending-записи и следующие редакции того же документа.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError
