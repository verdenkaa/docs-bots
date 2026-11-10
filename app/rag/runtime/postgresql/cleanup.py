"""Contracts: docs/documents/infrastructure/postgresql/cleanup.md."""


from typing import TypedDict
from uuid import UUID


class StagingCleanup(TypedDict):
    revision_id: UUID
    staging_ref: str


async def list_finished_staging(limit: int) -> list[StagingCleanup]:
    raise NotImplementedError


async def clear_staging_ref(revision_id: UUID, staging_ref: str) -> bool:
    raise NotImplementedError


async def is_staging_referenced(staging_ref: str) -> bool:
    raise NotImplementedError


async def claim_revision_cleanup(limit: int) -> list[UUID]:
    raise NotImplementedError


async def finish_revision_cleanup(revision_id: UUID) -> bool:
    raise NotImplementedError
