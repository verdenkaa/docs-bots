"""Conversion boundaries with no storage writes or model startup."""

import asyncio
from contextlib import ExitStack
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4, uuid5

from rag.contracts import StepError
from rag.ingestion import converter
from rag.ingestion._config import ConverterSettings
from rag.ingestion.contracts import ConversionResult, UploadInput, UploadSource
from rag.model_gateway.contracts import ImageDescription, OcrResult
from rag.runtime import s3


class ConverterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.revision = uuid4()
        self.marker = "@@IMG-test-{}@@"
        self.png = b"\x89PNG\r\n\x1a\n" + b"image"
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.settings = ConverterSettings(_env_file=None)
        self.stack.enter_context(patch.object(converter, "converter_settings", return_value=self.settings))
        self.parse = self.stack.enter_context(patch.object(converter, "_parse", new=AsyncMock(
            return_value=(self.marker.format(0), self.marker, [self.png]))))
        self.vision = self.stack.enter_context(patch.object(converter.model_gateway, "describe_image",
            new=AsyncMock(return_value=ImageDescription(profile_id="vision", description="Схема [A]|B"))))
        self.ocr = self.stack.enter_context(patch.object(converter.model_gateway, "recognize_text",
            new=AsyncMock(return_value=OcrResult(profile_id="ocr", text="Port 443"))))
        self.put = self.stack.enter_context(patch.object(s3, "put_image", new=AsyncMock()))
        self.get = self.stack.enter_context(patch.object(s3, "get_image", new=AsyncMock()))
        self.events = self.stack.enter_context(patch.object(converter, "emit_event"))

    def upload(self, parts=(b"# document",)):
        async def stream():
            for part in parts:
                yield part
        return UploadInput(content=stream(), source=UploadSource(filename="private.md", access_level="staff"),
                           document_id=None)

    async def run_conversion(self, parts=(b"# document",)):
        return await converter.convert_document(self.upload(parts), self.revision)

    async def test_returns_bytes_for_gateway_without_touching_s3(self):
        result = await self.run_conversion()
        self.assertIsInstance(result, ConversionResult)
        self.assertEqual(result.images[0].content.data, self.png)
        self.assertEqual(result.images[0].ref.image_id, uuid5(self.revision, "image:0"))
        self.assertIn(r"\[A\]\|B", result.markdown)
        self.put.assert_not_called()
        self.get.assert_not_called()

    async def test_no_images_does_not_call_models(self):
        self.parse.return_value = ("Text", self.marker, [])
        result = await self.run_conversion()
        self.assertEqual(result.images, [])
        self.vision.assert_not_called()
        self.ocr.assert_not_called()

    async def test_input_limits_and_invalid_stream_parts_abort_before_parser(self):
        self.settings.max_upload_bytes = 2
        for parts in ((b"abc",), (), ("text",)):
            with self.assertRaises(StepError):
                await self.run_conversion(parts)
        self.parse.assert_not_called()

    async def test_image_limits_and_invalid_marker_abort_before_models(self):
        for markdown, images in ((self.marker.format(2), [self.png]),
                                 (self.marker.format(0), [b"invalid"]),
                                 ("![x](image://forged)", [])):
            self.parse.return_value = (markdown, self.marker, images)
            with self.assertRaises(StepError):
                await self.run_conversion()
        self.vision.assert_not_called()

    async def test_duplicate_png_is_described_once_with_ids_in_text_order(self):
        self.parse.return_value = (self.marker.format(1) + self.marker.format(0), self.marker,
                                  [self.png, self.png])
        result = await self.run_conversion()
        self.assertEqual([item.ref.image_id for item in result.images],
                         [uuid5(self.revision, "image:0"), uuid5(self.revision, "image:1")])
        self.vision.assert_awaited_once()

    async def test_expanded_markdown_limit_is_checked(self):
        self.settings.max_markdown_bytes = 10
        with self.assertRaisesRegex(StepError, "too_large"):
            await self.run_conversion()

    async def test_mutated_model_result_is_revalidated(self):
        self.vision.return_value.description = ""
        with self.assertRaisesRegex(StepError, "model_invalid_response"):
            await self.run_conversion()

    async def test_timeout_is_retryable(self):
        self.settings.total_timeout_s = 0.01
        async def wait(*args, **kwargs):
            await asyncio.Event().wait()
        self.vision.side_effect = wait
        with self.assertRaises(StepError) as caught:
            await self.run_conversion()
        self.assertTrue(caught.exception.retryable)

    async def test_cancellation_propagates(self):
        self.vision.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self.run_conversion()

    async def test_description_is_bounded_and_cannot_forge_image_marker(self):
        self.vision.return_value.description = "image://forged " + "x" * 3000
        result = await self.run_conversion()
        self.assertLessEqual(len(result.images[0].ref.description), self.settings.max_description_chars)
        self.assertIn("…", result.images[0].ref.description)
        self.assertNotIn("private.md", str(self.events.call_args_list))
