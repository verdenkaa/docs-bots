"""Contracts: docs/documents/ingestion/entrypoints.md.

Ingestion Gateway: надёжный приём документа. Порядок шагов и действия при сбоях:
docs/documents/ingestion/gateway.md.
"""


from __future__ import annotations

from uuid import uuid4

from rag.contracts import StepError
from rag.ingestion.contracts import ConvertedDocument, UploadInput, UploadRejected, UploadResult, UploadSource
from rag.ingestion.converter import convert_document
from rag.runtime.postgresql.ingestion import register_revision
from rag.runtime.s3 import put_image
from rag.runtime.upload_spool import remove_staged_upload, stage_upload

# окончательные отказы конвертера, которые вызывающий получает как UploadRejected
_rejection_codes = ("invalid_upload", "too_large", "unsupported_format", "conversion_failed")


async def accept_upload(upload: UploadInput) -> UploadResult:
    """Конвертировать документ, подтвердить S3/Markdown и зарегистрировать задание с outbox.

    Args:
        upload: Поток и метаданные.

    Returns:
        Один вариант Pydantic UploadResult.
    """
    if not isinstance(upload, UploadInput):
        raise TypeError("Технический вход загрузки должен быть UploadInput")
    source = UploadSource.model_validate(upload.source)
    revision_id = uuid4()
    try:
        converted = await convert_document(upload, revision_id)
    except StepError as error:
        if error.retryable or error.code not in _rejection_codes:
            raise
        return UploadRejected(kind="rejected", code=error.code)

    # оригинал дальше не идёт: в S3 — картинки, в том — Markdown и манифест без байтов
    for image in converted.images:
        await put_image(image.ref.image_id, image.content)
    document = ConvertedDocument(markdown=converted.markdown, images=[image.ref for image in converted.images])
    staging_ref = await stage_upload(revision_id, document, source)
    # неизвестный исход коммита (StorageError) уходит вызывающему: Markdown и картинки остаются до сверки
    accepted = await register_revision(
        revision_id, upload.document_id, staging_ref, source.filename, [image.image_id for image in document.images])
    if accepted is None:
        # PostgreSQL подтвердил, что документа для замены нет: задания не будет, временный Markdown не нужен
        await remove_staged_upload(staging_ref)
        return UploadRejected(kind="rejected", code="unknown_document")
    return accepted
