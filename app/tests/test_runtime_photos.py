"""Real image decoding in the managed CPU executor, without Telegram or S3."""

import asyncio
from datetime import UTC, datetime, timedelta
from io import BytesIO
import unittest
from unittest.mock import patch

from PIL import Image

from rag.contracts import ImageContent, StepError
from rag.runtime import _state
from rag.runtime._config import RuntimeSettings
from rag.runtime._photos import _deadline
from rag.runtime.lifecycle import close_runtime
from rag.runtime.media import prepare_telegram_photo


def image_content(image, format="PNG", **kwargs):
    buffer = BytesIO()
    image.save(buffer, format=format, **kwargs)
    return ImageContent(
        data=buffer.getvalue(),
        mime_type="image/" + ("jpeg" if format == "JPEG" else format.lower()),
    )


class TelegramPhotoTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.resources = _state._Resources(RuntimeSettings(_env_file=None))
        _state._set(self.resources)
        _deadline.set(None)

    async def asyncTearDown(self):
        await close_runtime()

    async def check_photo(self, content):
        result = await prepare_telegram_photo(content)
        self.assertEqual(result.mime_type, "image/jpeg")
        self.assertLessEqual(len(result.data), 10_000_000)
        with Image.open(BytesIO(result.data)) as photo:
            photo.load()
            self.assertEqual(photo.mode, "RGB")
            self.assertLessEqual(photo.width + photo.height, 10000)
            self.assertLessEqual(max(photo.size), 20 * min(photo.size))
            return photo.copy()

    async def test_transparency_white_background_and_original_unchanged(self):
        original = image_content(Image.new("RGBA", (20, 20), (255, 0, 0, 0)))
        saved = original.data
        photo = await self.check_photo(original)
        self.assertEqual(original.data, saved)
        self.assertTrue(all(channel > 245 for channel in photo.getpixel((0, 0))))

    async def test_exif_orientation_and_cmyk(self):
        source = Image.new("RGB", (30, 10), "red")
        exif = source.getexif()
        exif[274] = 6
        photo = await self.check_photo(image_content(source, "JPEG", exif=exif))
        self.assertEqual(photo.size, (10, 30))
        self.assertEqual(
            (await self.check_photo(image_content(Image.new("CMYK", (10, 10)), "JPEG"))).size,
            (10, 10),
        )

    async def test_extreme_ratio_adds_fields_and_large_dimensions_shrink(self):
        photo = await self.check_photo(image_content(Image.new("RGB", (1, 12000), "black")))
        self.assertGreater(photo.width, 1)
        self.assertLess(photo.height, 12000)
        self.assertTrue(
            all(channel < 30 for channel in photo.getpixel((photo.width // 2, photo.height // 2)))
        )
        self.assertTrue(all(channel > 245 for channel in photo.getpixel((0, photo.height // 2))))

    async def test_corrupt_input_and_pixel_limit_are_permanent(self):
        for content in (
            ImageContent(data=b"invalid", mime_type="image/png"),
            ImageContent(
                data=b"\x89PNG\r\n\x1a\n" + b"\0" * 4 + b"IHDR" + b"\0" * 8, mime_type="image/png"
            ),
        ):
            with self.assertRaisesRegex(StepError, "telegram_photo_invalid") as raised:
                await prepare_telegram_photo(content)
            self.assertFalse(raised.exception.retryable)
        self.resources.settings.photo_max_pixels = 100
        with self.assertRaisesRegex(StepError, "telegram_photo_invalid"):
            await prepare_telegram_photo(image_content(Image.new("RGB", (20, 20))))

    async def test_high_entropy_photo_fits_telegram_byte_limit(self):
        noisy = Image.effect_noise((4000, 4000), 100)
        content = image_content(noisy)
        self.assertLessEqual(len(content.data), self.resources.settings.max_image_bytes)
        photo = await self.check_photo(content)
        self.assertEqual(photo.size, (4000, 4000))

    async def test_cancel_stops_executor_before_returning(self):
        real_spawn = asyncio.create_subprocess_exec
        created = asyncio.Event()
        processes = []

        async def hang(*arguments, **options):
            process = await real_spawn(arguments[0], "-c", "import time; time.sleep(30)", **options)
            processes.append(process)
            created.set()
            return process

        with patch("rag.runtime._photos.asyncio.create_subprocess_exec", side_effect=hang):
            task = asyncio.create_task(
                prepare_telegram_photo(image_content(Image.new("RGB", (5, 5))))
            )
            await asyncio.wait_for(created.wait(), timeout=5)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertIsNotNone(processes[0].returncode)
        self.assertFalse(self.resources.photos)

    async def test_delivery_deadline_limits_time_and_executor_failure_is_retryable(self):
        _deadline.set(datetime.now(UTC) - timedelta(seconds=1))
        with self.assertRaisesRegex(StepError, "telegram_photo_executor_unavailable") as raised:
            await prepare_telegram_photo(image_content(Image.new("RGB", (5, 5))))
        self.assertTrue(raised.exception.retryable)
        _deadline.set(None)
        with patch(
            "rag.runtime._photos.asyncio.create_subprocess_exec", side_effect=OSError("Injected")
        ):
            with self.assertRaisesRegex(StepError, "telegram_photo_executor_unavailable") as raised:
                await prepare_telegram_photo(image_content(Image.new("RGB", (5, 5))))
        self.assertTrue(raised.exception.retryable)
