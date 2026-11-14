"""Contracts: docs/documents/infrastructure/rabbitmq/contracts.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations


async def relay_outbox_batch(limit: int) -> int:
    """Опубликовать готовую пачку outbox и отметить подтверждённые сообщения в БД.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        limit: Положительный предел размера пачки.

    Returns:
        Одно целое число.
    """
    raise NotImplementedError
