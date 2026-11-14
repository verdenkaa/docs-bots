"""Delivery-scoped, cancellable CPU work with bounded process concurrency."""

import asyncio
from contextvars import ContextVar
from datetime import UTC, datetime
import os
import subprocess
import sys

from rag.contracts import ImageContent, StepError
from rag.runtime._state import _get
from rag.runtime._validation import _image

_deadline = ContextVar("runtime_photo_delivery_deadline", default=None)


async def _stop_photo(process):
    if process.returncode is None:
        try:
            process.kill()
        except ProcessLookupError:
            pass
    await process.wait()


async def _close_photos(resources):
    await asyncio.gather(*(_stop_photo(process) for process in tuple(resources.photos)))


async def _prepare_photo(image):
    image = ImageContent.model_validate(image.model_dump())
    resources = _get()
    settings = resources.settings
    if len(image.data) > settings.max_image_bytes:
        raise StepError("telegram_photo_invalid", False)
    try:
        _image(image.data, image.mime_type)
    except StepError:
        raise StepError("telegram_photo_invalid", False) from None
    timeout = min(settings.photo_timeout_s, settings.delivery_timeout_s)
    deadline = _deadline.get()
    if deadline is not None:
        timeout = min(timeout, max(0, (deadline - datetime.now(UTC)).total_seconds()))
    if timeout <= 0:
        raise StepError("telegram_photo_executor_unavailable", True)
    try:
        async with asyncio.timeout(timeout):
            async with resources.photo_lock:
                environment = {
                    key: value
                    for key, value in os.environ.items()
                    if key.upper()
                    in ("PATH", "SYSTEMROOT", "WINDIR", "TEMP", "TMP", "LANG", "PYTHONPATH")
                }
                options = (
                    {"creationflags": subprocess.CREATE_NO_WINDOW}
                    if sys.platform == "win32"
                    else {}
                )
                spawning = asyncio.create_task(
                    asyncio.create_subprocess_exec(
                        sys.executable,
                        "-m",
                        "rag.runtime._photo_worker",
                        str(settings.photo_max_pixels),
                        str(settings.photo_memory_bytes),
                        str(settings.max_image_bytes),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.DEVNULL,
                        env=environment,
                        **options,
                    )
                )
                try:
                    process = await asyncio.shield(spawning)
                except BaseException:
                    # Cancellation during spawn must not lose the newly created PID.
                    try:
                        process = await spawning
                    except Exception:
                        pass
                    else:
                        await _stop_photo(process)
                    raise
                resources.photos.add(process)
                try:
                    output, _ = await process.communicate(image.data)
                    if process.returncode == 2:
                        raise StepError("telegram_photo_invalid", False)
                    if process.returncode != 0:
                        raise StepError("telegram_photo_executor_unavailable", True)
                    if not output or len(output) > 10_000_000:
                        raise StepError("telegram_photo_invalid", False)
                    return ImageContent(data=output, mime_type="image/jpeg")
                finally:
                    await _stop_photo(process)
                    resources.photos.discard(process)
    except (OSError, TimeoutError):
        raise StepError("telegram_photo_executor_unavailable", True) from None
