"""Contracts: docs/documents/ingestion/entrypoints.md.

Document Converter: формат выбирается по содержимому, PDF и DOCX разбираются в управляемом
процессе, описания картинок приходят из Model Gateway. Порядок: docs/documents/ingestion/converter.md.
"""


from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
import zipfile
from io import BytesIO
from uuid import UUID, uuid4, uuid5

from pydantic import ValidationError

from rag.contracts import ImageContent, ImageRef, StepError
from rag.ingestion._config import ConverterSettings, converter_settings
from rag.ingestion.contracts import ConversionResult, ConvertedImage, UploadInput
from rag.model_gateway import entrypoints as model_gateway
from rag.runtime.events import emit_event

_markdown_suffixes = (".md", ".markdown")


async def convert_document(upload: UploadInput, revision_id: UUID) -> ConversionResult:
    """Привести документ к Markdown и извлечь изображения для сохранения Gateway в S3.

    Args:
        upload: Поток читается один раз с лимитами.
        revision_id: Основа стабильных ID изображений.

    Returns:
        Один Pydantic ConversionResult, без оригинала.
    """
    from rag.ingestion._conversion import _validate_upload
    upload = _validate_upload(upload, revision_id)
    try:
        settings = converter_settings()
    except ValidationError:
        raise StepError("ingestion_configuration_error", False) from None
    started = time.monotonic()
    log: dict[str, object] = {"job_id": str(revision_id), "outcome": "failed"}
    try:
        async with asyncio.timeout(settings.total_timeout_s):
            data = await _read_upload(upload, settings.max_upload_bytes)
            kind = log["format"] = _detect_format(data, upload.source.filename)
            markdown, marker, images = await _parse(kind, data, settings, log)
            del data
            result = await _assemble(markdown, marker, images, revision_id, settings)
            if len(result.markdown.encode("utf-8")) > settings.max_markdown_bytes:
                raise StepError("too_large", False)
        log.update(outcome="converted", images=len(result.images))
        return result
    except TimeoutError:
        log["error_code"] = "converter_unavailable"
        raise StepError("converter_unavailable", True) from None
    except StepError as error:
        log["error_code"] = error.code
        raise
    except asyncio.CancelledError:
        log["outcome"] = "cancelled"
        raise
    except Exception:
        log["error_code"] = "conversion_failed"
        raise StepError("conversion_failed", False) from None
    finally:
        emit_event("ingestion_gateway", "convert_document", "finished", uuid4().hex,
                   duration_ms=round((time.monotonic() - started) * 1000), **log)


async def _read_upload(upload: UploadInput, maximum: int) -> bytes:
    """Прочитать поток в память; на диск оригинал не попадает."""
    buffer = bytearray()
    async for chunk in upload.content:
        if not isinstance(chunk, bytes):
            raise StepError("invalid_upload", False)
        if len(buffer) + len(chunk) > maximum:
            raise StepError("too_large", False)
        buffer.extend(chunk)
    if not buffer:
        raise StepError("invalid_upload", False)
    return bytes(buffer)


def _detect_format(data: bytes, filename: str) -> str:
    """Выбрать обработчик по содержимому; имя файла решает только для Markdown, у которого нет сигнатуры."""
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                is_docx = "word/document.xml" in archive.namelist()
        except zipfile.BadZipFile:
            raise StepError("conversion_failed", False) from None
        if is_docx:
            return "docx"
    elif filename.lower().endswith(_markdown_suffixes) and b"\x00" not in data:
        return "md"
    raise StepError("unsupported_format", False)


async def _parse(kind: str, data: bytes, settings: ConverterSettings, log: dict[str, object]) -> tuple[str, str, list[bytes]]:
    """Parse in a bounded child; the converter never writes S3."""
    from rag.ingestion._document_parser import _run_worker

    marker = f"@@IMG-{secrets.token_hex(16)}-{{}}@@"
    markdown, images = await _run_worker(kind, data, marker, settings)
    return markdown, marker, images


async def _assemble(markdown: str, marker: str, images: list[bytes], revision_id: UUID,
                    settings: ConverterSettings) -> ConversionResult:
    """Назначить image_id в порядке текста, получить описания и поставить плейсхолдеры."""
    if "image://" in markdown:
        # чужой текст не должен выглядеть как наш плейсхолдер изображения
        raise StepError("conversion_failed", False)
    marker_re = re.compile(re.escape(marker).replace(r"\{\}", r"(\d+)"))
    order = list(dict.fromkeys(int(number) for number in marker_re.findall(markdown)))
    if any(number >= len(images) for number in order):
        raise StepError("conversion_failed", False)
    if len(order) > settings.max_images or any(len(images[number]) > settings.max_image_bytes for number in order):
        raise StepError("too_large", False)
    if any(not images[number].startswith(b"\x89PNG\r\n\x1a\n") for number in order):
        raise StepError("conversion_failed", False)

    descriptions = await _describe_images([images[number] for number in order], settings)
    converted: dict[int, ConvertedImage] = {}
    for image_no, (number, description) in enumerate(zip(order, descriptions)):
        converted[number] = ConvertedImage(
            ref=ImageRef(image_id=uuid5(revision_id, f"image:{image_no}"), description=description),
            content=ImageContent(data=images[number], mime_type="image/png"),
        )

    def placeholder(match: re.Match[str]) -> str:
        ref = converted[int(match.group(1))].ref
        # описание экранируется по Markdown; "|" — чтобы плейсхолдер не ломал строку таблицы
        escaped = re.sub(r"([\\\[\]|])", r"\\\1", ref.description)
        escaped = escaped.replace("image://", "image\\://")
        return f"![{escaped}](image://{ref.image_id})"

    markdown = marker_re.sub(placeholder, markdown).strip()
    try:
        return ConversionResult(markdown=markdown + "\n", images=[converted[number] for number in order])
    except ValidationError:
        # пустой Markdown или несовпадение плейсхолдеров со списком изображений
        raise StepError("conversion_failed", False) from None


async def _describe_images(images: list[bytes], settings: ConverterSettings) -> list[str]:
    """Описание каждой картинки; одинаковые картинки (значки кнопок) описываются один раз."""
    from rag.model_gateway.contracts import ImageDescription, OcrResult
    from rag.ingestion._conversion import _validated

    limit = asyncio.Semaphore(settings.model_concurrency)

    async def describe(data: bytes) -> str:
        image = ImageContent(data=data, mime_type="image/png")
        async with limit:
            described = await model_gateway.describe_image(image, timeout_s=settings.describe_timeout_s)
            recognized = await model_gateway.recognize_text(image, timeout_s=settings.ocr_timeout_s)
        described = _validated(ImageDescription, described, "model_invalid_response")
        recognized = _validated(OcrResult, recognized, "model_invalid_response")
        text = " ".join(described.description.split())
        if recognized.text.strip():
            text = f"{text.rstrip('.')}. Текст на изображении: {' '.join(recognized.text.split())}"
        if len(text) > settings.max_description_chars:
            text = text[:settings.max_description_chars - 1].rstrip() + "…"
        return text

    tasks: dict[str, asyncio.Task[str]] = {}
    keys = [hashlib.sha256(data).hexdigest() for data in images]
    for key, data in zip(keys, images):
        if key not in tasks:
            tasks[key] = asyncio.create_task(describe(data))
    try:
        await asyncio.gather(*tasks.values())
    finally:
        for task in tasks.values():
            task.cancel()
        await asyncio.gather(*tasks.values(), return_exceptions=True)
    return [tasks[key].result() for key in keys]
