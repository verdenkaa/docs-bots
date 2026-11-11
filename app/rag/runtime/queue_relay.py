"""Contracts: docs/documents/infrastructure/rabbitmq/contracts.md."""


async def relay_outbox_batch(limit: int) -> int:
    """Опубликовать готовую пачку outbox и отметить подтверждённые сообщения в БД.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Количество уведомлений, подтверждённых брокером и отмеченных в PostgreSQL.
        Сбой оставляет работу для повтора; возможные дубли подавляет потребитель.
    """
    raise NotImplementedError
