"""Contracts: docs/documents/infrastructure/rabbitmq/contracts.md."""


from typing import Literal, TypedDict
from uuid import UUID

from rag.contracts import RequestRef
from rag.ingestion.contracts import JobRef


WorkRoute = Literal["rag.questions", "rag.results", "ingestion.jobs"]


class RagNotice(TypedDict):
    message_id: UUID
    route: Literal["rag.questions", "rag.results"]
    event: RequestRef


class IngestionNotice(TypedDict):
    message_id: UUID
    route: Literal["ingestion.jobs"]
    event: JobRef


OutboxNotice = RagNotice | IngestionNotice


class BrokerPublishError(Exception):
    """The broker did not confirm a routed publication."""


async def publish_notice(notice: OutboxNotice) -> None:
    raise NotImplementedError
