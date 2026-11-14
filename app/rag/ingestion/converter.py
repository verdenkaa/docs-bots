"""Contracts: docs/documents/ingestion/entrypoints.md.

Document Converter: формат выбирается по содержимому, PDF и DOCX разбираются в управляемом
процессе, описания картинок приходят из Model Gateway. Порядок: docs/documents/ingestion/converter.md.
"""


from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import secrets
import signal
import struct
import sys
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
_refusal_codes = {2: "conversion_failed", 3: "unsupported_format"}
# переменные, без которых не работают интерпретатор и кеш моделей docling; секреты приложения не передаются
_worker_environment = (
    "PATH", "PYTHONPATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP", "SYSTEMROOT", "WINDIR",
    "HF_HOME", "HF_HUB_OFFLINE", "XDG_CACHE_HOME", "DOCLING_ARTIFACTS_PATH",
)


async def convert_document(upload: UploadInput, revision_id: UUID) -> ConversionResult:
    """Привести документ к Markdown и извлечь изображения для сохранения Gateway в S3.

    Args:
        upload: Поток читается один раз с лимитами.
        revision_id: Основа стабильных ID изображений.

    Returns:
        Один Pydantic ConversionResult, без оригинала.
    """
    settings = converter_settings()
    started = time.monotonic()
    log: dict[str, object] = {"job_id": str(revision_id), "outcome": "failed"}
    try:
        async with asyncio.timeout(settings.total_timeout_s):
            data = await _read_upload(upload, settings.max_upload_bytes)
            kind = log["format"] = _detect_format(data, upload.source.filename)
            markdown, marker, images = await _parse(kind, data, settings, log)
            del data
            result = await _assemble(markdown, marker, images, revision_id, settings)
        log.update(outcome="converted", images=len(result.images))
        return result
    except TimeoutError:
        log["error_code"] = "converter_unavailable"
        raise StepError("converter_unavailable", True) from None
    except StepError as error:
        log["error_code"] = error.code
        raise
    finally:
        emit_event("ingestion_gateway", "convert_document", "finished", uuid4().hex,
                   duration_ms=round((time.monotonic() - started) * 1000), **log)


async def _read_upload(upload: UploadInput, maximum: int) -> bytes:
    """Прочитать поток в память; на диск оригинал не попадает."""
    buffer = bytearray()
    async for chunk in upload.content:
        buffer += chunk
        if len(buffer) > maximum:
            raise StepError("too_large", False)
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
    """Вернуть Markdown, шаблон метки картинки и PNG-картинки в порядке номеров меток."""
    marker = f"@@IMG-{secrets.token_hex(8)}-{{}}@@"
    if kind == "md":
        from rag.ingestion._parsers import PARSERS, ParseRefused

        try:
            return PARSERS[kind].parse(data, lambda _: ""), marker, []
        except ParseRefused as refusal:
            log["reason"] = refusal.reason
            raise StepError(refusal.code, False) from None

    code, output, errors = await _run_worker(kind, data, marker, settings)
    if code != 0:
        lines = errors.decode("utf-8", "replace").strip().splitlines()
        log["reason"] = lines[-1][:300] if lines else f"код выхода {code}"
        if code in _refusal_codes:
            raise StepError(_refusal_codes[code], False)
        raise StepError("converter_unavailable", True)
    try:
        (size,) = struct.unpack(">I", output[:4])
        header = json.loads(output[4:4 + size])
        position = 4 + size
        parts = []
        for length in [header["markdown"], *header["images"]]:
            parts.append(output[position:position + length])
            position += length
        if position != len(output):
            raise ValueError("лишние или недостающие байты результата")
        return parts[0].decode("utf-8"), marker, parts[1:]
    except (struct.error, ValueError, KeyError, TypeError):
        log["reason"] = "исполнитель вернул неразборчивый результат"
        raise StepError("converter_unavailable", True) from None


async def _run_worker(kind: str, data: bytes, marker: str, settings: ConverterSettings) -> tuple[int, bytes, bytes]:
    """Разобрать документ в отдельной группе процессов и остановить её целиком при сроке или отмене."""
    command = (sys.executable, "-m", "rag.ingestion._convert_worker", kind,
               str(settings.parse_memory_bytes), str(len(data)), marker)
    environment = {key: value for key, value in os.environ.items() if key.upper() in _worker_environment}
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        *command,
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        env=environment, start_new_session=sys.platform != "win32",
    ))
    try:
        process = await asyncio.shield(spawning)
    except OSError:
        raise StepError("converter_unavailable", True) from None
    except BaseException:
        # отмена во время запуска не должна потерять уже созданный процесс
        try:
            process = await spawning
        except Exception:  # noqa: BLE001 - процесс не создан, останавливать нечего
            pass
        else:
            await _stop(process)
        raise
    try:
        async with asyncio.timeout(settings.parse_timeout_s):
            output, errors = await process.communicate(data)
        return process.returncode, output, errors
    except TimeoutError:
        raise StepError("converter_unavailable", True) from None
    finally:
        await _stop(process)


async def _stop(process) -> None:
    """Убить процесс разбора вместе с потомками; отмены корутины для этого недостаточно."""
    try:
        if sys.platform == "win32":
            if process.returncode is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    await process.wait()


async def _assemble(markdown: str, marker: str, images: list[bytes], revision_id: UUID,
                    settings: ConverterSettings) -> ConversionResult:
    """Назначить image_id в порядке текста, получить описания и поставить плейсхолдеры."""
    if "image://" in markdown:
        # чужой текст не должен выглядеть как наш плейсхолдер изображения
        raise StepError("conversion_failed", False)
    marker_re = re.compile(re.escape(marker).replace(r"\{\}", r"(\d+)"))
    order = [int(number) for number in marker_re.findall(markdown)]
    if len(order) != len(set(order)) or any(number >= len(images) for number in order):
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
        return f"![{escaped}](image://{ref.image_id})"

    markdown = marker_re.sub(placeholder, markdown).strip()
    try:
        return ConversionResult(markdown=markdown + "\n", images=[converted[number] for number in order])
    except ValidationError:
        # пустой Markdown или несовпадение плейсхолдеров со списком изображений
        raise StepError("conversion_failed", False) from None


async def _describe_images(images: list[bytes], settings: ConverterSettings) -> list[str]:
    """Описание каждой картинки; одинаковые картинки (значки кнопок) описываются один раз."""
    limit = asyncio.Semaphore(settings.model_concurrency)

    async def describe(data: bytes) -> str:
        image = ImageContent(data=data, mime_type="image/png")
        async with limit:
            described = await model_gateway.describe_image(image, timeout_s=settings.describe_timeout_s)
            recognized = await model_gateway.recognize_text(image, timeout_s=settings.ocr_timeout_s)
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
    return [tasks[key].result() for key in keys]
