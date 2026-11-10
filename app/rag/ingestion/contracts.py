"""Contracts: docs/documents/ingestion/contracts.md."""


from collections.abc import AsyncIterable
from datetime import datetime
from pathlib import Path
from typing import Literal, TypedDict
from uuid import UUID

from rag.contracts import SourceLocator


class UploadSource(TypedDict):
    filename: str
    source_url: str | None


class UploadInput(TypedDict):
    content: AsyncIterable[bytes]
    source: UploadSource
    document_id: UUID | None


class JobRef(TypedDict):
    job_id: UUID


class AcceptedUpload(JobRef):
    kind: Literal["accepted"]
    document_id: UUID


class UploadRejected(TypedDict):
    kind: Literal["rejected"]
    code: Literal["unknown_document", "invalid_upload", "too_large"]


UploadResult = AcceptedUpload | UploadRejected


class StagedUpload(TypedDict):
    path: Path
    source: UploadSource


class IngestionAttempt(JobRef):
    attempt_no: int
    deadline_at: datetime


class ClaimedIngestionJob(TypedDict):
    attempt: IngestionAttempt
    document_id: UUID
    staging_ref: str


class PreparedChunk(TypedDict):
    text: str
    locator: SourceLocator | None

