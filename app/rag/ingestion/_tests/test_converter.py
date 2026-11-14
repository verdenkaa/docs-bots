"""Conversion boundaries without S3 connections, model startup or local files."""

import asyncio
from contextlib import ExitStack
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4, uuid5

from rag.contracts import ImageContent, StepError, StorageError
from rag.ingestion import converter
from rag.ingestion._conversion import _ConversionSettings, _load_converter
from rag.ingestion.contracts import ConvertedDocument, UploadInput, UploadSource
from rag.model_gateway.contracts import ImageDescription, OcrResult


async def _configured_adapter(upload, revision_id):
    async for _ in upload.content:
        pass
    return "Document"


class ConverterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.revision = uuid4()
        self.image = uuid5(self.revision, "image:0")
        self.url = f"s3://rag-images/images/{self.image}"
        self.markdown = f"# Guide\n\n![Raw caption]({self.url})\n\n| Port | 443 |\n"
        self.settings = _ConversionSettings(converter_callable="test:convert",
                                            ocr_profile="ocr-v1", vision_profile="vision-v1")
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.object(converter, "_get", return_value=SimpleNamespace(
            settings=SimpleNamespace(s3_bucket="rag-images"))))
        self.stack.enter_context(patch.object(_ConversionSettings, "from_environment",
                                            side_effect=lambda: self.settings))
        self.adapter = self.stack.enter_context(patch.object(
            converter, "_load_converter", return_value=AsyncMock(side_effect=self._adapter))).return_value
        self.get_image = self.stack.enter_context(patch(
            "rag.ingestion._conversion.s3.get_image", new=AsyncMock(
                return_value=ImageContent(data=b"image", mime_type="image/png"))))
        self.ocr = self.stack.enter_context(patch(
            "rag.ingestion._conversion.models.recognize_text", new=AsyncMock(
                return_value=OcrResult(profile_id="ocr-v1", text="HTTPS\n443"))))
        self.vision = self.stack.enter_context(patch(
            "rag.ingestion._conversion.models.describe_image", new=AsyncMock(
                return_value=ImageDescription(profile_id="vision-v1", description="Схема подключения"))))
        self.events = self.stack.enter_context(patch.object(converter, "emit_event"))

    async def _adapter(self, upload, revision_id):
        self.assertEqual(revision_id, self.revision)
        self.input_parts = [part async for part in upload.content]
        return self.markdown

    def _upload(self, parts=(b"original",)):
        async def stream():
            for part in parts:
                yield part
        return UploadInput(content=stream(), source=UploadSource(
            filename="Private.docx", access_level="employees"), document_id=None)

    async def _convert(self, parts=(b"original",)):
        return await converter.convert_document(self._upload(parts), self.revision)

    async def test_document_is_enriched_without_bytes_in_result(self):
        result = await self._convert((b"one", b"two"))
        self.assertIsInstance(result, ConvertedDocument)
        self.assertEqual(self.input_parts, [b"one", b"two"])
        self.assertEqual(result.images[0].description,
                         "Схема подключения Распознанный текст: HTTPS 443")
        self.assertEqual(result.markdown,
                         f"# Guide\n\n![{result.images[0].description}](image://{self.image})\n\n| Port | 443 |\n")
        self.assertNotIn("data", str(result.model_dump()))
        self.get_image.assert_awaited_once_with(self.image)
        self.adapter.assert_awaited_once()

    async def test_duplicate_references_reuse_one_image_and_description(self):
        self.markdown += f"\n![Same]({self.url})"
        result = await self._convert()
        self.assertEqual(len(result.images), 1)
        self.assertEqual(result.markdown.count(f"](image://{self.image})"), 2)
        self.get_image.assert_awaited_once()
        self.ocr.assert_awaited_once()
        self.vision.assert_awaited_once()

    async def test_no_images_does_not_call_image_or_model_ports(self):
        self.markdown = "# Plain text\n\nValue: AUTH-17"
        result = await self._convert()
        self.assertEqual((result.markdown, result.images), (self.markdown, []))
        self.get_image.assert_not_awaited()
        self.ocr.assert_not_awaited()
        self.vision.assert_not_awaited()

    async def test_code_examples_comments_and_escaped_images_are_preserved(self):
        example = "![Example](s3://foreign-bucket/images/example)"
        prefix = f"```md\n{example}\n```\n`{example}`\n<!-- {example} -->\n\\{example}\n"
        self.markdown = prefix + self.markdown
        result = await self._convert()
        self.assertTrue(result.markdown.startswith(prefix))
        self.assertEqual(len(result.images), 1)

    async def test_angle_destination_title_and_escaped_caption(self):
        self.markdown = f'![old \\] caption](<{self.url}> "Title")'
        result = await self._convert()
        self.assertEqual(len(result.images), 1)
        self.assertNotIn("Title", result.markdown)

    async def test_images_in_lists_are_processed_and_list_code_is_preserved(self):
        example = "![Example](s3://foreign-bucket/images/example)"
        self.markdown = f"- Step:\n    ![Image]({self.url})\n\n    ```md\n    {example}\n    ```\n"
        result = await self._convert()
        self.assertEqual(len(result.images), 1)
        self.assertIn(f"    ![{result.images[0].description}](image://{self.image})", result.markdown)
        self.assertIn(example, result.markdown)

    async def test_ocr_can_be_empty_and_description_is_markdown_escaped(self):
        self.ocr.return_value = OcrResult(profile_id="ocr-v1", text="")
        self.vision.return_value = ImageDescription(
            profile_id="vision-v1", description=r"Путь C:\[Data] и image://example")
        result = await self._convert()
        self.assertEqual(result.images[0].description, self.vision.return_value.description)
        self.assertIn(r"C:\\", result.markdown)
        self.assertIn(r"\[Data\]", result.markdown)
        self.assertIn(r"image\://example", result.markdown)

    async def test_foreign_noncanonical_or_unresolved_images_fail_before_download(self):
        for destination in ("https://example.test/image.png", "../image.png",
                            self.url.replace("rag-images", "other-bucket"),
                            self.url + "?signature=secret", self.url + "#fragment",
                            f"s3://rag-images/images/{uuid4()}"):
            with self.subTest(destination=destination):
                self.markdown = f"![Image]({destination})"
                with self.assertRaisesRegex(StepError, "conversion_failed"):
                    await self._convert()
        self.get_image.assert_not_awaited()

    async def test_reference_or_html_image_is_rejected_explicitly(self):
        for markdown in (f"![Image][asset]\n\n[asset]: {self.url}",
                         f'<img src="{self.url}">', "![broken]("):
            with self.subTest(markdown=markdown):
                self.markdown = markdown
                with self.assertRaisesRegex(StepError, "conversion_failed"):
                    await self._convert()
        self.get_image.assert_not_awaited()

    async def test_input_limits_cover_unread_tail_and_bad_stream_parts(self):
        async def header_only(upload, revision):
            await anext(aiter(upload.content))
            return "Document"
        self.adapter.side_effect = header_only
        self.settings = self.settings.model_copy(update={"max_upload_bytes": 3})
        with self.assertRaisesRegex(StepError, "too_large"):
            await self._convert((b"a", b"trailing bytes"))
        for parts in ((), ("text rather than bytes",)):
            with self.subTest(parts=parts), self.assertRaisesRegex(StepError, "invalid_upload"):
                await self._convert(parts)

    async def test_empty_or_oversized_markdown_does_not_start_models(self):
        for markdown in ("", " \n", None):
            self.markdown = markdown
            with self.subTest(markdown=markdown), self.assertRaisesRegex(StepError, "conversion_failed"):
                await self._convert()
        self.markdown = "Ж" * 20
        self.settings = self.settings.model_copy(update={"max_markdown_bytes": 30})
        with self.assertRaisesRegex(StepError, "too_large"):
            await self._convert()
        self.get_image.assert_not_awaited()

    async def test_description_and_expanded_document_are_never_truncated(self):
        self.settings = self.settings.model_copy(update={"max_description_characters": 3})
        with self.assertRaisesRegex(StepError, "image_description_too_large"):
            await self._convert()
        self.settings = self.settings.model_copy(update={
            "max_description_characters": 10000,
            "max_markdown_bytes": len(self.markdown.encode()) + 1,
        })
        self.vision.return_value = ImageDescription(profile_id="vision-v1", description="Длинное " * 100)
        with self.assertRaisesRegex(StepError, "too_large"):
            await self._convert()

    async def test_model_profile_or_mutated_result_is_rejected(self):
        self.ocr.return_value = OcrResult(profile_id="wrong", text="")
        with self.assertRaisesRegex(StepError, "model_profile_mismatch"):
            await self._convert()
        self.ocr.return_value = OcrResult.model_construct(profile_id="ocr-v1", text=42)
        with self.assertRaisesRegex(StepError, "model_invalid_response"):
            await self._convert()

    async def test_image_failure_aborts_whole_document_and_keeps_error_policy(self):
        second = uuid5(self.revision, "image:1")
        self.markdown += f"\n![Second](s3://rag-images/images/{second})"
        self.get_image.side_effect = [ImageContent(data=b"image", mime_type="image/png"),
                                      StepError("s3_unavailable", True)]
        with self.assertRaises(StepError) as raised:
            await self._convert()
        self.assertEqual((raised.exception.code, raised.exception.retryable), ("s3_unavailable", True))
        self.assertEqual(self.vision.await_count, 1)

    async def test_total_timeout_and_cancellation_propagation(self):
        async def wait(*args):
            await asyncio.Event().wait()
        self.adapter.side_effect = wait
        self.settings = self.settings.model_copy(update={"timeout_s": 0.01})
        with self.assertRaisesRegex(StepError, "conversion_timeout"):
            await self._convert()
        self.adapter.side_effect = asyncio.CancelledError
        with self.assertRaises(asyncio.CancelledError):
            await self._convert()

    async def test_image_and_model_steps_have_their_own_timeouts(self):
        async def wait(*args, **kwargs):
            await asyncio.Event().wait()
        self.settings = self.settings.model_copy(update={"image_timeout_s": 0.01, "model_timeout_s": 0.01})
        self.get_image.side_effect = wait
        with self.assertRaisesRegex(StepError, "image_timeout"):
            await self._convert()
        self.get_image.side_effect = None
        self.ocr.side_effect = wait
        with self.assertRaisesRegex(StepError, "model_timeout"):
            await self._convert()

    async def test_image_count_and_size_limits_abort_before_publication(self):
        second = uuid5(self.revision, "image:1")
        self.markdown += f"\n![Second](s3://rag-images/images/{second})"
        self.settings = self.settings.model_copy(update={"max_images": 1})
        with self.assertRaisesRegex(StepError, "too_large"):
            await self._convert()
        self.get_image.assert_not_awaited()
        self.settings = self.settings.model_copy(update={"max_images": 2, "max_image_bytes": 1})
        with self.assertRaisesRegex(StepError, "too_large"):
            await self._convert()
        self.ocr.assert_not_awaited()

    async def test_unknown_storage_outcome_is_not_an_expected_conversion_rejection(self):
        self.adapter.side_effect = StorageError("unconfirmed upload")
        with self.assertRaises(StorageError):
            await self._convert()

    async def test_adapter_error_and_logs_do_not_expose_document_or_urls(self):
        self.adapter.side_effect = RuntimeError("Private.docx " + self.url)
        with self.assertRaises(StepError) as raised:
            await self._convert()
        self.assertEqual(str(raised.exception), "conversion_failed")
        self.assertIsNone(raised.exception.__cause__)
        self.assertNotIn("Private.docx", str(self.events.call_args_list))
        self.assertNotIn(self.url, str(self.events.call_args_list))


class ConverterConfigurationTests(unittest.TestCase):
    def test_real_entrypoint_loads_configured_async_adapter(self):
        async def stream():
            yield b"original"
        upload = UploadInput(content=stream(), source=UploadSource(
            filename="Guide.docx", access_level="employees"), document_id=None)
        with patch.dict(os.environ, dict(INGESTION_CONVERTER_CALLABLE=f"{__name__}:_configured_adapter",
                                        INGESTION_OCR_PROFILE="ocr", INGESTION_VISION_PROFILE="vision"), clear=True), \
                patch.object(converter, "_get", return_value=SimpleNamespace(
                    settings=SimpleNamespace(s3_bucket="rag-images"))), \
                patch.object(converter, "emit_event"):
            result = asyncio.run(converter.convert_document(upload, uuid4()))
        self.assertEqual(result, ConvertedDocument(markdown="Document", images=[]))

    def test_required_environment_has_no_profile_or_converter_fallback(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(
                StepError, "ingestion_configuration_error"):
            _ConversionSettings.from_environment()
        with patch.dict(os.environ, dict(INGESTION_CONVERTER_CALLABLE=f"{__name__}:_configured_adapter",
                                        INGESTION_OCR_PROFILE="ocr", INGESTION_VISION_PROFILE="vision"), clear=True):
            settings = _ConversionSettings.from_environment()
        self.assertIs(_load_converter(settings.converter_callable), _configured_adapter)

    def test_non_async_or_missing_adapter_is_rejected(self):
        for path in ("missing", "missing_module_for_converter:convert", "os:getcwd"):
            with self.subTest(path=path), self.assertRaisesRegex(StepError, "ingestion_configuration_error"):
                _load_converter(path)
