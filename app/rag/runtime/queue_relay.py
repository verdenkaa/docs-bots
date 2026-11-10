"""Contracts: docs/documents/infrastructure/rabbitmq/contracts.md."""


async def relay_outbox_batch(limit: int) -> int:
    raise NotImplementedError
