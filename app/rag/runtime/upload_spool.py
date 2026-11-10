"""Contracts: docs/documents/infrastructure/upload-spool/contracts.md."""


from collections.abc import AsyncIterable
from datetime import datetime
from uuid import UUID

from rag.ingestion.contracts import StagedUpload, UploadRejected, UploadSource


async def stage_upload(
    revision_id: UUID,
    content: AsyncIterable[bytes],
    source: UploadSource,
) -> str | UploadRejected:
    raise NotImplementedError


async def open_staged_upload(staging_ref: str) -> StagedUpload | None:
    raise NotImplementedError


async def remove_staged_upload(staging_ref: str) -> None:
    raise NotImplementedError


async def list_staging_candidates(older_than: datetime, limit: int) -> list[str]:
    raise NotImplementedError
