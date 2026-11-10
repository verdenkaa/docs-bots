"""Contracts: docs/documents/ingestion/entrypoints.md."""


from rag.ingestion.contracts import UploadInput, UploadResult


async def accept_upload(upload: UploadInput) -> UploadResult:
    raise NotImplementedError
