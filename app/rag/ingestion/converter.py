"""Contracts: docs/documents/ingestion/entrypoints.md.

Prepare Markdown whose extracted images have already been stored in S3.
The configured async adapter owns format parsing and image uploads; it must
offload CPU parsing and manage any child processes within the call's lifetime.
"""

from __future__ import annotations

import asyncio
from time import monotonic
from uuid import UUID, uuid4

from rag.contracts import StepError, StorageError
from rag.ingestion._conversion import (
    _ConversionSettings, _ImageEnricher, _LimitedContent, _load_converter,
    _validate_upload,
)
from rag.ingestion._image_markdown import _image_links, _replace_images
from rag.ingestion.contracts import ConvertedDocument, UploadInput
from rag.runtime.events import _event_context, emit_event
from rag.runtime._state import _get


async def convert_document(upload: UploadInput, revision_id: UUID) -> ConvertedDocument:
    """Convert once, read S3 images, add OCR/VLM descriptions and image:// markers.

    Required environment: INGESTION_CONVERTER_CALLABLE=module:function,
    INGESTION_OCR_PROFILE and INGESTION_VISION_PROFILE. The adapter receives
    (UploadInput, revision_id), returns str and confirms all image writes before
    returning. Its canonical links are s3://<Runtime bucket>/images/<image_id>;
    IDs are uuid5(revision_id, 'image:' + str(number)), numbered from zero.
    No original or extracted image is written to a local directory here.
    """
    upload = _validate_upload(upload, revision_id)
    settings = _ConversionSettings.from_environment()
    adapter = _load_converter(settings.converter_callable)
    if adapter is convert_document:
        raise StepError("ingestion_configuration_error", False)
    run_id, started = str(uuid4()), monotonic()
    outcome, error_code = "failed", None
    with _event_context(job_id=str(revision_id), revision_id=str(revision_id)):
        emit_event("ingestion_gateway", "convert_document", "started", run_id)
        try:
            async with asyncio.timeout(settings.timeout_s):
                content = _LimitedContent(upload.content, settings.max_upload_bytes)
                bounded = UploadInput(content=content, source=upload.source,
                                      document_id=upload.document_id)
                markdown = await adapter(bounded, revision_id)
                await content.finish()
                if not isinstance(markdown, str) or not markdown.strip():
                    raise StepError("conversion_failed", False)
                if len(markdown.encode("utf-8")) > settings.max_markdown_bytes:
                    raise StepError("too_large", False)
                links = _image_links(markdown, revision_id, _get().settings.s3_bucket,
                                     settings.max_images)
                images = await _ImageEnricher(settings).describe(links)
                prepared = _replace_images(markdown, links, images)
                if len(prepared.encode("utf-8")) > settings.max_markdown_bytes:
                    raise StepError("too_large", False)
                document = ConvertedDocument(markdown=prepared, images=images)
            outcome = "completed"
            return document
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except TimeoutError:
            error_code = "conversion_timeout"
            raise StepError(error_code, True) from None
        except StepError as error:
            error_code = error.code
            raise
        except StorageError:
            error_code = "storage_unconfirmed"
            raise
        except Exception:
            # Adapter/parser exceptions may contain document text or S3 URLs.
            error_code = "conversion_failed"
            raise StepError(error_code, False) from None
        finally:
            emit_event("ingestion_gateway", "convert_document", "finished", run_id,
                       outcome=outcome, error_code=error_code,
                       duration_ms=round((monotonic() - started) * 1000))
