"""Raw PDF/DOCX adapter: original stream -> Markdown with confirmed S3 images.

Select with INGESTION_CONVERTER_CALLABLE=
rag.ingestion._document_parser:_convert_to_markdown.
OCR/VLM and image:// placeholders belong to the existing outer convert_document.
"""

from __future__ import annotations

import asyncio
from io import BytesIO
import json
import os
import re
import secrets
import signal
import struct
import sys
import zipfile
from uuid import UUID, uuid4, uuid5

from rag.contracts import ImageContent, StepError
from rag.ingestion._conversion import _ConversionSettings, _validate_upload
from rag.ingestion._image_markdown import _image_links
from rag.ingestion.contracts import UploadInput
from rag.runtime import s3
from rag.runtime._state import _get
from rag.runtime.events import _event_context, emit_event


_WORKER_ENV = {"PATH", "PYTHONPATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
               "SYSTEMROOT", "WINDIR"}
_EXIT_ERRORS = {2: "conversion_failed", 3: "unsupported_format", 5: "too_large",
                6: "ingestion_configuration_error"}


async def _convert_to_markdown(upload: UploadInput, revision_id: UUID) -> str:
    upload = _validate_upload(upload, revision_id)
    settings = _ConversionSettings.from_environment()
    run_id, outcome, error_code = str(uuid4()), "failed", None
    with _event_context(job_id=str(revision_id), revision_id=str(revision_id)):
        emit_event("ingestion_gateway", "parse_document", "started", run_id)
        try:
            async with asyncio.timeout(settings.timeout_s):
                data = bytearray()
                async for part in upload.content:
                    if not isinstance(part, bytes):
                        raise StepError("invalid_upload", False)
                    if len(data) + len(part) > settings.max_upload_bytes:
                        raise StepError("too_large", False)
                    data.extend(part)
                if not data:
                    raise StepError("invalid_upload", False)
                kind = _detect_format(data, upload.source.filename)
                marker = "@@IMG-" + secrets.token_hex(16) + "-{}@@"
                markdown, images = await _run_worker(kind, bytes(data), marker, settings)
                del data
                result = await _upload_images(markdown, marker, images, revision_id, settings)
            outcome = "completed"
            return result
        except TimeoutError:
            error_code = "converter_unavailable"
            raise StepError(error_code, True) from None
        except StepError as error:
            error_code = error.code
            raise
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        finally:
            emit_event("ingestion_gateway", "parse_document", "finished", run_id,
                       outcome=outcome, error_code=error_code)


def _detect_format(data, filename):
    if b"%PDF-" in data[:1024]:
        return "pdf"
    if data.startswith(b"PK\x03\x04"):
        try:
            with zipfile.ZipFile(BytesIO(data)) as archive:
                if "word/document.xml" in archive.namelist():
                    return "docx"
        except zipfile.BadZipFile:
            raise StepError("conversion_failed", False) from None
    elif filename.lower().endswith((".md", ".markdown")) and b"\0" not in data:
        return "md"
    raise StepError("unsupported_format", False)


async def _read_bounded(stream, maximum):
    result = bytearray()
    while True:
        block = await stream.read(min(65536, maximum - len(result) + 1))
        if not block:
            return bytes(result)
        result.extend(block)
        if len(result) > maximum:
            raise StepError("too_large", False)


async def _send_input(process, data):
    try:
        process.stdin.write(data)
        await process.stdin.drain()
    except (BrokenPipeError, ConnectionResetError):
        pass  # an early parser refusal is classified by the exit code
    finally:
        process.stdin.close()


async def _stop(process):
    try:
        if sys.platform == "win32":
            if process.returncode is None:
                process.kill()
        else:
            os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    # Drain remaining pipe buffers after the group is dead, so wait cannot
    # deadlock on a paused stdout/stderr transport.
    await asyncio.wait_for(process.communicate(), timeout=5)


async def _settle(task):
    """Finish subprocess cleanup even if the caller cancels it again."""
    cancelled = False
    while True:
        try:
            result = await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            cancelled = True
    if cancelled:
        raise asyncio.CancelledError
    return result


async def _run_worker(kind, data, marker, settings):
    command = (sys.executable, "-m", "rag.ingestion._convert_worker", kind,
               str(settings.parse_memory_bytes), str(len(data)), marker,
               str(settings.max_markdown_bytes), str(settings.max_images),
               str(settings.max_image_bytes), str(settings.max_parser_output_bytes))
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={key: value for key, value in os.environ.items() if key.upper() in _WORKER_ENV},
        start_new_session=sys.platform != "win32",
    ))
    try:
        process = await asyncio.shield(spawning)
    except OSError:
        raise StepError("converter_unavailable", True) from None
    except BaseException:
        # A cancellation during spawn must not orphan a process created later.
        async def finish_spawn():
            try:
                created = await spawning
            except Exception:
                return
            await _stop(created)
        await _settle(asyncio.create_task(finish_spawn()))
        raise
    tasks = [asyncio.create_task(_send_input(process, data)),
             asyncio.create_task(_read_bounded(process.stdout, settings.max_parser_output_bytes)),
             asyncio.create_task(_read_bounded(process.stderr, 65536))]
    try:
        async with asyncio.timeout(settings.parse_timeout_s):
            _, output, _ = await asyncio.gather(*tasks)
            code = await process.wait()
        if code in _EXIT_ERRORS:
            raise StepError(_EXIT_ERRORS[code], False)
        if code != 0:
            raise StepError("converter_unavailable", True)
        return _decode_output(output, settings)
    except TimeoutError:
        raise StepError("converter_unavailable", True) from None
    finally:
        async def finish():
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await _stop(process)
        await _settle(asyncio.create_task(finish()))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate parser header key")
        result[key] = value
    return result


def _decode_output(output, settings):
    try:
        if len(output) > settings.max_parser_output_bytes:
            raise ValueError()
        size, = struct.unpack(">I", output[:4])
        if size > 128 * 1024 or 4 + size > len(output):
            raise ValueError()
        header = json.loads(output[4:4 + size], object_pairs_hook=_unique_object)
        if not isinstance(header, dict) or set(header) != {"markdown", "images"}:
            raise ValueError()
        if not isinstance(header["images"], list) or len(header["images"]) > settings.max_images:
            raise ValueError()
        lengths = [header["markdown"], *header["images"]]
        if any(type(length) is not int or length < 0 for length in lengths):
            raise ValueError()
        if lengths[0] > settings.max_markdown_bytes or any(
                length == 0 or length > settings.max_image_bytes for length in lengths[1:]):
            raise ValueError()
        position = 4 + size
        if position + sum(lengths) != len(output):
            raise ValueError()
        parts = []
        for length in lengths:
            parts.append(output[position:position + length])
            position += length
        return parts[0].decode("utf-8"), parts[1:]
    except (ValueError, KeyError, TypeError, struct.error):
        raise StepError("converter_unavailable", True) from None


async def _upload_images(markdown, marker, images, revision_id, settings):
    if not markdown.strip() or "image://" in markdown:
        raise StepError("conversion_failed", False)
    pattern = re.compile(re.escape(marker).replace(r"\{\}", r"(\d+)"))
    order = list(dict.fromkeys(int(match[1]) for match in pattern.finditer(markdown)))
    if any(number >= len(images) for number in order):
        raise StepError("conversion_failed", False)
    if len(order) > settings.max_images or any(len(images[number]) > settings.max_image_bytes for number in order):
        raise StepError("too_large", False)
    if any(not images[number].startswith(b"\x89PNG\r\n\x1a\n") for number in order):
        raise StepError("conversion_failed", False)
    bucket = _get().settings.s3_bucket
    identifiers = {number: uuid5(revision_id, "image:" + str(index)) for index, number in enumerate(order)}
    result = pattern.sub(lambda match: f"![Image](s3://{bucket}/images/{identifiers[int(match[1])]})", markdown)
    if len(result.encode("utf-8")) > settings.max_markdown_bytes:
        raise StepError("too_large", False)
    # Validate the complete result before the first external write.
    links = _image_links(result, revision_id, bucket, settings.max_images)
    if {link[2] for link in links} != set(identifiers.values()):
        raise StepError("conversion_failed", False)
    for number, image_id in identifiers.items():
        try:
            async with asyncio.timeout(settings.image_timeout_s):
                await s3.put_image(image_id, ImageContent(data=images[number], mime_type="image/png"))
        except TimeoutError:
            raise StepError("image_timeout", True) from None
    return result
