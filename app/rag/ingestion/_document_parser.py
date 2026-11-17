"""Private bounded parser process; no S3 or model calls."""

from __future__ import annotations

import asyncio
import json
import os
import signal
import struct
import sys
import tempfile
from contextlib import nullcontext

from rag.contracts import StepError

_WORKER_ENV = {"PATH", "PYTHONPATH", "HOME", "LANG", "LC_ALL", "TMPDIR", "TEMP", "TMP",
               "SYSTEMROOT", "WINDIR"}
_EXIT_ERRORS = {2: "conversion_failed", 3: "unsupported_format", 5: "too_large",
                6: "ingestion_configuration_error"}


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
    # The parent owns renderer metadata so SIGKILL of the parser cannot leave
    # temporary Inkscape profiles behind. Original bytes never enter this tree.
    workspace = tempfile.TemporaryDirectory(prefix="rag-docx-") if kind == "docx" else nullcontext(None)
    with workspace as directory:
        return await _run_worker_process(kind, data, marker, settings, directory)


async def _run_worker_process(kind, data, marker, settings, directory):
    command = (sys.executable, "-m", "rag.ingestion._convert_worker", kind,
               str(settings.parse_memory_bytes), str(len(data)), marker,
               str(settings.max_markdown_bytes), str(settings.max_images),
               str(settings.max_image_bytes), str(settings.max_parser_output_bytes))
    env = {key: value for key, value in os.environ.items() if key.upper() in _WORKER_ENV}
    if directory is not None:
        env["_RAG_CONVERTER_WORK_DIRECTORY"] = directory
    spawning = asyncio.create_task(asyncio.create_subprocess_exec(
        *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
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
