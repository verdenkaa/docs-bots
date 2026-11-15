"""Private validation helpers for conversion boundaries."""

from collections.abc import AsyncIterable
from uuid import UUID
from pydantic import ValidationError
from rag.contracts import StepError
from rag.ingestion.contracts import UploadInput, UploadSource


def _validate_upload(upload: UploadInput, revision_id: UUID) -> UploadInput:
    try:
        if (not isinstance(upload, UploadInput) or not isinstance(revision_id, UUID)
                or not isinstance(upload.content, AsyncIterable)
                or (upload.document_id is not None and not isinstance(upload.document_id, UUID))):
            raise ValueError()
        source = UploadSource.model_validate(upload.source.model_dump()
                                            if isinstance(upload.source, UploadSource) else upload.source)
        return UploadInput(content=upload.content, source=source, document_id=upload.document_id)
    except (ValueError, TypeError, AttributeError):
        raise StepError("invalid_upload", False) from None


def _validated(model, raw, error_code):
    try:
        return model.model_validate(raw.model_dump(warnings=False) if isinstance(raw, model) else raw)
    except (ValidationError, ValueError, TypeError):
        raise StepError(error_code, False) from None
