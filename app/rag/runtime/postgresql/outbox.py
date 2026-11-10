"""Contracts: docs/documents/infrastructure/postgresql/outbox.md."""


from datetime import datetime
from typing import TypedDict
from uuid import UUID

from rag.runtime.rabbitmq import OutboxNotice, WorkRoute


class ReadyOutboxRow(TypedDict):
    notice: OutboxNotice
    available_at: datetime


async def list_ready_outbox(limit: int) -> list[ReadyOutboxRow]:
    raise NotImplementedError


async def mark_outbox_published(message_id: UUID) -> bool:
    raise NotImplementedError


async def recover_pending_work(route: WorkRoute, limit: int) -> int:
    raise NotImplementedError
