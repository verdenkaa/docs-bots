"""Contracts: docs/documents/ingestion/entrypoints.md."""


from rag.contracts import ConsumeDecision
from rag.ingestion.contracts import JobRef, PreparedChunk, StagedUpload


async def handle_ingestion_job(event: JobRef) -> ConsumeDecision:
    raise NotImplementedError


async def prepare_chunks(upload: StagedUpload) -> list[PreparedChunk]:
    raise NotImplementedError
