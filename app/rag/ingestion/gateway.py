"""Contracts: docs/documents/ingestion/entrypoints.md.

Приём через существующие контракты конвертера, разбиения и Runtime.
"""


from __future__ import annotations

import asyncio
from collections.abc import AsyncIterable
from uuid import UUID, uuid4

from pydantic import TypeAdapter, ValidationError

from rag.contracts import StepError, StorageError
from rag.ingestion._gateway_settings import _GatewaySettings
from rag.ingestion.contracts import (
    AcceptedUpload, ConversionResult, ConvertedDocument, DocumentBatch, PreparedChunk,
    UploadInput, UploadRejected, UploadResult, UploadSource,
)
from rag.ingestion.converter import convert_document
from rag.ingestion.worker import prepare_chunks
from rag.runtime.events import _event_context, emit_event
from rag.runtime.postgresql.ingestion import register_revision
from rag.runtime.s3 import put_image
from rag.runtime.upload_spool import (
    load_document_batch, open_staged_upload, remove_staged_upload, stage_upload, store_document_batch,
)


class _BoundedStream:
    def __init__(self, content, limit):
        self.content = content
        self.limit = limit
        self.size = 0
        self.finished = False
        self.started = False

    def __aiter__(self):
        if self.started:
            raise StepError("invalid_upload", False)
        self.started = True
        return self._iterate()

    async def _iterate(self):
        async for chunk in self.content:
            if not isinstance(chunk, bytes):
                raise StepError("invalid_upload", False)
            self.size += len(chunk)
            if self.size > self.limit:
                raise StepError("too_large", False)
            if chunk:
                yield chunk
        self.finished = True
        if not self.size:
            raise StepError("invalid_upload", False)


async def accept_upload(upload: UploadInput) -> UploadResult:
    """Конвертировать документ, подтвердить S3/Markdown и зарегистрировать задание с outbox.

    Args:
        upload: Поток и метаданные.

    Returns:
        Один вариант Pydantic UploadResult.
    """
    if not isinstance(upload, UploadInput) or not isinstance(upload.content, AsyncIterable):
        return UploadRejected(kind="rejected", code="invalid_upload")
    try:
        source = UploadSource.model_validate(
            upload.source.model_dump() if isinstance(upload.source, UploadSource) else upload.source
        )
    except ValidationError:
        return UploadRejected(kind="rejected", code="invalid_upload")
    if upload.document_id is not None and not isinstance(upload.document_id, UUID):
        return UploadRejected(kind="rejected", code="invalid_upload")

    settings = _GatewaySettings()
    stream = _BoundedStream(upload.content, settings.max_upload_bytes)
    bounded = UploadInput(content=stream, source=source, document_id=upload.document_id)
    revision_id = uuid4()
    emit_event("ingestion_gateway", "accept_upload", "started", str(revision_id),
               job_id=str(revision_id))
    outcome = "technical_error"
    registration_started = False
    try:
        async with asyncio.timeout(settings.upload_timeout_s):
            try:
                with _event_context(job_id=str(revision_id)):
                    converted = await convert_document(bounded, revision_id)
            except StepError as exc:
                if not exc.retryable and exc.code in {
                    "invalid_upload", "too_large", "unsupported_format", "conversion_failed",
                }:
                    outcome = exc.code
                    return UploadRejected(kind="rejected", code=exc.code)
                raise
            if not stream.finished:
                raise StepError("converter_incomplete_input", False)
            converted = ConversionResult.model_validate(converted.model_dump(warnings=False))
            for image in converted.images:
                await put_image(image.ref.image_id, image.content)
            document = ConvertedDocument(
                markdown=converted.markdown, images=[image.ref for image in converted.images],
            )
            staging_ref = await stage_upload(revision_id, document, source)
            if staging_ref != f"uploads/{revision_id}":
                raise StorageError("Staged revision identity mismatch")
            staged = await open_staged_upload(staging_ref)
            if staged is None:
                raise StorageError("Staged upload is unavailable")
            if staged.source != source or staged.images != document.images:
                raise StorageError("Staged upload metadata mismatch")
            chunks = TypeAdapter(list[PreparedChunk]).validate_python([
                chunk.model_dump() for chunk in await prepare_chunks(staged)])
            if not chunks:
                outcome = "conversion_failed"
                return UploadRejected(kind="rejected", code="conversion_failed")
            batch = DocumentBatch(source=source, chunks=chunks)
            try:
                await store_document_batch(staging_ref, batch)
            except (StorageError, TimeoutError):
                restored = await load_document_batch(staging_ref)
                if restored is None or DocumentBatch.model_validate_json(restored.model_dump_json()) != batch:
                    raise StorageError("Document batch storage was not confirmed") from None
            registration_started = True
            accepted = await register_revision(
                revision_id, upload.document_id, staging_ref, source,
                [image.image_id for image in document.images],
            )
            if accepted is None:
                await remove_staged_upload(staging_ref)
                outcome = "unknown_document"
                return UploadRejected(kind="rejected", code="unknown_document")
            accepted = AcceptedUpload.model_validate_json(accepted.model_dump_json())
            if (accepted.job_id != revision_id or (upload.document_id is not None
                    and accepted.document_id != upload.document_id)):
                raise StorageError("Registered revision identity mismatch")
            outcome = "accepted"
            return accepted
    except TimeoutError:
        if registration_started:
            raise StorageError("Upload registration outcome is unknown") from None
        raise StepError("upload_timeout", True) from None
    finally:
        # No exception messages, filenames, content or credentials enter this event.
        emit_event("ingestion_gateway", "accept_upload", "finished", str(revision_id),
                   job_id=str(revision_id), outcome=outcome)
