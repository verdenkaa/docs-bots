"""Contracts: docs/documents/infrastructure/postgresql/ingestion.md."""


from datetime import datetime
from typing import TypedDict
from uuid import UUID

from rag.ingestion.contracts import AcceptedUpload, ClaimedIngestionJob, IngestionAttempt


class PendingRevisionWrite(TypedDict):
    revision_id: UUID
    attempt_no: int


class RevisionManifest(TypedDict):
    revision_id: UUID
    expected_chunk_count: int | None


async def register_revision(
    revision_id: UUID,
    document_id: UUID | None,
    staging_ref: str,
) -> AcceptedUpload | None:
    raise NotImplementedError


async def claim_ingestion_job(job_id: UUID) -> ClaimedIngestionJob | None:
    raise NotImplementedError


async def begin_revision_write(attempt: IngestionAttempt, expected_chunk_count: int) -> bool:
    raise NotImplementedError


async def publish_revision(attempt: IngestionAttempt) -> int | None:
    raise NotImplementedError


async def schedule_ingestion_retry(attempt: IngestionAttempt, retry_at: datetime) -> bool:
    raise NotImplementedError


async def fail_ingestion_job(attempt: IngestionAttempt) -> bool:
    raise NotImplementedError


async def list_pending_revision_writes(limit: int) -> list[PendingRevisionWrite]:
    raise NotImplementedError


async def clear_revision_write_pending(revision_id: UUID, attempt_no: int) -> bool:
    raise NotImplementedError


async def list_required_revision_manifests() -> list[RevisionManifest]:
    raise NotImplementedError
