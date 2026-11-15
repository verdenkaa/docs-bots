"""Document Converter: convert_document на собранных вручную PDF и DOCX, готовом Markdown и отказах."""

import asyncio
import contextlib
import importlib.util
import io
import os
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
from uuid import UUID, uuid5

from sample_docx import build_sample_docx
from sample_pdf import IMAGE_SIZE, LINK_URI, build_sample_pdf

from rag.contracts import ImageContent, StepError
from rag.ingestion import _conversion, _document_parser, _parsers, converter
from rag.ingestion._config import ConverterSettings
from rag.ingestion.contracts import UploadInput, UploadSource
from rag.model_gateway.contracts import ImageDescription, OcrResult
from rag.runtime import _state, s3

HARD_BREAK = "  \n"
HAS_PDF_LIBS = all(importlib.util.find_spec(name) for name in ("pdfplumber", "pypdf"))
REVISION = UUID("7d0a2a4e-58f5-4e3c-9d5e-3f2b9c1a6b10")
IMAGE_ID = uuid5(REVISION, "image:0")


def settings(**overrides):
    return ConverterSettings(_env_file=None, **overrides)


EXPECTED_MARKDOWN = f"""\
## 1 Getting started

Plain text and **bold text** in one paragraph.

### 1.1 Details

1. First step
    > Note inside list
2. Second step
    - nested item

See [the docs]({LINK_URI})

| **Name** | **Value** |
| --- | --- |
| Budget | Deviation |

> Important note

Press ![Кнопка \\[Сохранить\\]. Текст на изображении: Save](image://{IMAGE_ID}) to save.

Type - kind{HARD_BREAK}Model - model of item
"""

EXPECTED_DOCX_MARKDOWN = f"""\
# Sample document

## Getting started

Plain text and **bold text** in one paragraph.

### Details

1. First step
    Note inside list
    - nested item
2. Second step

See [the docs]({LINK_URI})

| **Name** | **Value** |
| --- | --- |
| Filter | !> 200 \\| x |
| Merged |  |

> Important note

Press ![Кнопка \\[Сохранить\\]. Текст на изображении: Save](image://{IMAGE_ID}) to save.

Type - kind{HARD_BREAK}Model - model of item
"""


def upload(data: bytes, filename: str, chunk: int = 1000) -> UploadInput:
    async def stream():
        for start in range(0, len(data), chunk):
            yield data[start:start + chunk]

    return UploadInput(content=stream(), source=UploadSource(filename=filename, access_level="employees"), document_id=None)


class ConverterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.describe = mock.AsyncMock(return_value=ImageDescription(profile_id="vision", description="Кнопка [Сохранить]."))
        self.recognize = mock.AsyncMock(return_value=OcrResult(profile_id="ocr", text="Save"))
        for name, fake in (("describe_image", self.describe), ("recognize_text", self.recognize)):
            patcher = mock.patch.object(converter.model_gateway, name, fake)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.settings(settings())
        stdout = contextlib.redirect_stdout(io.StringIO())  # события логов
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def settings(self, value: ConverterSettings) -> None:
        patcher = mock.patch.object(converter, "converter_settings", return_value=value)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def refused(self, data: bytes, filename: str) -> StepError:
        with self.assertRaises(StepError) as caught:
            await converter.convert_document(upload(data, filename), REVISION)
        return caught.exception

    @unittest.skipUnless(HAS_PDF_LIBS, "нужны pdfplumber и pypdf")
    async def test_tagged_pdf_becomes_markdown_with_image_placeholder(self) -> None:
        from PIL import Image

        result = await converter.convert_document(upload(build_sample_pdf(tagged=True), "doc.pdf"), REVISION)

        # заголовки, жирный, вложенный список, ссылка, таблица, рамки, картинка посреди предложения и явный
        # перенос строки; оглавления и колонтитула нет; описание картинки — VLM + OCR, экранировано
        self.assertEqual(result.markdown, EXPECTED_MARKDOWN)
        (image,) = result.images
        self.assertEqual(image.ref.image_id, IMAGE_ID)
        self.assertEqual(image.ref.description, "Кнопка [Сохранить]. Текст на изображении: Save")
        self.assertEqual(image.content.mime_type, "image/png")
        with Image.open(io.BytesIO(image.content.data)) as picture:
            self.assertEqual(picture.size, IMAGE_SIZE)

    @unittest.skipUnless(HAS_PDF_LIBS, "нужны pdfplumber и pypdf")
    def test_pdf_without_tags_preserves_text_and_images(self) -> None:
        data = build_sample_pdf(tagged=False)
        images = []
        def add_image(png):
            images.append(png)
            return "IMAGE"
        markdown = _parsers.PARSERS["pdf"].parse(data, add_image)
        self.assertIn("Getting started", markdown)
        self.assertIn("Plain text and bold text in one paragraph.", markdown)
        self.assertEqual(markdown.count("IMAGE"), 1)
        self.assertEqual(len(images), 1)

    @unittest.skipUnless(HAS_PDF_LIBS, "нужны pdfplumber и pypdf")
    async def test_pdf_without_tags_reaches_ocr_and_vision(self) -> None:
        result = await converter.convert_document(upload(build_sample_pdf(tagged=False), "plain.pdf"), REVISION)
        self.assertEqual(len(result.images), 1)
        self.assertIn(f"](image://{IMAGE_ID})", result.markdown)
        self.assertIn("Текст на изображении: Save", result.markdown)
        self.describe.assert_awaited_once()
        self.recognize.assert_awaited_once()

    @unittest.skipUnless(HAS_PDF_LIBS, "нужны pdfplumber и pypdf")
    async def test_scanned_pdf_is_refused_before_ocr_and_vision(self) -> None:
        data = build_sample_pdf(tagged=False, page_content=b"q 300 0 0 500 50 100 cm /Im1 Do Q")
        error = await self.refused(data, "scan.pdf")
        self.assertEqual((error.code, error.retryable), ("unsupported_format", False))
        self.describe.assert_not_called()
        self.recognize.assert_not_called()

    @unittest.skipUnless(HAS_PDF_LIBS, "нужны pdfplumber и pypdf")
    async def test_repeated_pdf_image_has_two_placeholders_and_one_model_analysis(self) -> None:
        data = build_sample_pdf(tagged=False, page_content=(
            b"BT /F1 11 Tf 50 700 Td (Pictures) Tj ET\n"
            b"q 20 0 0 20 50 600 cm /Im1 Do Q\nq 20 0 0 20 50 500 cm /Im1 Do Q"))
        result = await converter.convert_document(upload(data, "images.pdf"), REVISION)
        self.assertEqual([image.ref.image_id for image in result.images],
                         [uuid5(REVISION, "image:0"), uuid5(REVISION, "image:1")])
        for image in result.images:
            self.assertIn(f"](image://{image.ref.image_id})", result.markdown)
        self.describe.assert_awaited_once()
        self.recognize.assert_awaited_once()

    @unittest.skipUnless(HAS_PDF_LIBS, "нужны pdfplumber и pypdf")
    async def test_pdf_image_limit_is_enforced_before_models(self) -> None:
        self.settings(settings(max_images=1))
        data = build_sample_pdf(tagged=False, page_content=(
            b"BT /F1 11 Tf 50 700 Td (Pictures) Tj ET\n"
            b"q 20 0 0 20 50 600 cm /Im1 Do Q\nq 20 0 0 20 50 500 cm /Im1 Do Q"))
        error = await self.refused(data, "images.pdf")
        self.assertEqual((error.code, error.retryable), ("too_large", False))
        self.describe.assert_not_called()
        self.recognize.assert_not_called()

    @unittest.skipUnless(HAS_PDF_LIBS, "нужны pdfplumber и pypdf")
    async def test_broken_pdf_is_final_failure(self) -> None:
        error = await self.refused(b"%PDF-1.7\nnot a pdf at all", "broken.pdf")
        self.assertEqual((error.code, error.retryable), ("conversion_failed", False))

    async def test_docx_becomes_markdown_with_image_placeholder(self) -> None:
        from PIL import Image

        result = await converter.convert_document(upload(build_sample_docx(), "doc.docx"), REVISION)

        # название и заголовки из стилей, без оглавления и удалённого текста; список по нумерации Word с абзацем
        # внутри пункта; ссылка, таблица с оператором фильтра и объединённой ячейкой, рамка, картинка в предложении
        self.assertEqual(result.markdown, EXPECTED_DOCX_MARKDOWN)
        (image,) = result.images
        self.assertEqual(image.ref.image_id, IMAGE_ID)
        self.assertEqual(image.content.mime_type, "image/png")
        with Image.open(io.BytesIO(image.content.data)) as picture:
            self.assertEqual(picture.size, IMAGE_SIZE)

    async def test_docx_format_is_detected_by_content_not_name(self) -> None:
        result = await converter.convert_document(upload(build_sample_docx(), "doc.pdf"), REVISION)
        self.assertEqual(result.markdown, EXPECTED_DOCX_MARKDOWN)

    async def test_docx_with_dtd_is_final_failure(self) -> None:
        data = build_sample_docx(document_prefix='<!DOCTYPE d [<!ENTITY e "x">]>')
        error = await self.refused(data, "doc.docx")
        self.assertEqual((error.code, error.retryable), ("conversion_failed", False))

    async def test_hung_parser_is_stopped(self) -> None:
        self.settings(settings().model_copy(update={"parse_timeout_s": 0.05}))
        spawning = asyncio.create_subprocess_exec
        children = []

        async def hung(*command, **options):
            child = await spawning(sys.executable, "-c", "import time; time.sleep(60)", **options)
            children.append(child)
            return child

        with mock.patch.object(_document_parser.asyncio, "create_subprocess_exec", side_effect=hung):
            error = await self.refused(build_sample_docx(), "doc.docx")
        self.assertEqual((error.code, error.retryable), ("converter_unavailable", True))
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        if sys.platform != "win32":
            with self.assertRaises(ProcessLookupError):
                os.killpg(children[0].pid, 0)

    async def test_markdown_passes_through(self) -> None:
        text = "# Заголовок\n\n| a | b |\n| --- | --- |\n| 1 | !> 200 |\n"
        result = await converter.convert_document(upload(text.encode(), "Руководство.MD"), REVISION)

        self.assertEqual(result.markdown, text)
        self.assertEqual(result.images, [])
        self.describe.assert_not_called()

    async def test_markdown_external_image_stays_text_without_network_access(self):
        text = "![image](https://example.test/image.png)\n"
        result = await converter.convert_document(upload(text.encode(), "doc.md"), REVISION)
        self.assertEqual(result.markdown, text)
        self.assertEqual(result.images, [])
        self.describe.assert_not_called()

    async def test_refusals(self) -> None:
        cases = [
            (b"", "empty.md", "invalid_upload"),
            (b"plain text", "notes.txt", "unsupported_format"),
            (b"PK\x03\x04 not a zip", "doc.docx", "conversion_failed"),
            (b"   \n", "blank.md", "conversion_failed"),
            ("![x](image://not-ours)".encode(), "forged.md", "conversion_failed"),
            (b"\xff\xfe\xfa", "binary.md", "conversion_failed"),
        ]
        for data, filename, code in cases:
            with self.subTest(filename=filename):
                error = await self.refused(data, filename)
                self.assertEqual((error.code, error.retryable), (code, False))

    async def test_upload_over_limit_is_too_large(self) -> None:
        self.settings(settings(max_upload_bytes=10))
        error = await self.refused(b"# long markdown", "doc.md")
        self.assertEqual((error.code, error.retryable), ("too_large", False))

    async def test_repeated_image_reference_is_described_once(self) -> None:
        from sample_docx import _png
        png = _png()
        marker = "@@IMG-test-{}@@"
        markdown = f"{marker.format(0)} и {marker.format(0)}"
        with mock.patch.object(converter.secrets, "token_hex", return_value="test"), \
                mock.patch.object(_document_parser, "_run_worker", return_value=(markdown, [png])):
            result = await converter.convert_document(upload(b"# original", "doc.md"), REVISION)
        self.assertEqual(self.describe.await_count, 1)
        self.assertEqual([image.ref.image_id for image in result.images], [IMAGE_ID])
        self.assertEqual(result.markdown.count(f"](image://{IMAGE_ID})"), 2)

    async def test_model_failure_stays_technical(self) -> None:
        self.describe.side_effect = StepError("model_unavailable", True)
        with self.assertRaises(StepError) as caught:
            await converter.convert_document(upload(build_sample_docx(), "doc.docx"), REVISION)
        self.assertEqual((caught.exception.code, caught.exception.retryable), ("model_unavailable", True))



class ConverterWithModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    """convert_document без подмены describe_image/recognize_text: вызовы идут через настоящий Model Gateway."""

    async def asyncSetUp(self) -> None:
        import os
        import tempfile

        from model_gateway_fixtures import response

        from rag.model_gateway.contracts import ModelBinding
        from rag.model_gateway.entrypoints import close_models, start_models
        from rag.model_gateway.providers import ModelAdapter, register_provider, unregister_provider

        calls = self.calls = []

        class Adapter(ModelAdapter):
            execution = "remote"

            def __init__(self, binding, role):
                self.binding, self.role = binding, role

            async def start(self):
                pass

            async def invoke(self, operation, payload, *, timeout_s):
                calls.append((operation, payload["image"]["mime_type"]))
                return response(self.binding, self.role, operation, payload)

            async def close(self):
                pass

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        environment = mock.patch.dict(os.environ, {
            "MODEL_GATEWAY_CACHE_DIR": temp.name, "MODEL_GATEWAY_PROVIDER_MODULES": "[]",
            "MODEL_GATEWAY_LOCAL_LOADERS": "{}",
        })
        environment.start()
        self.addCleanup(environment.stop)
        stdout = contextlib.redirect_stdout(io.StringIO())  # события логов
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)
        patcher = mock.patch.object(converter, "converter_settings", return_value=settings())
        patcher.start()
        self.addCleanup(patcher.stop)

        name = "converter_fixture"
        register_provider(name, Adapter)
        self.addCleanup(unregister_provider, name)
        await start_models({
            role: ModelBinding(
                profile_id=role, execution="remote", provider=name, model_id="fixture", model_revision="1",
                endpoint="https://models.example.test", credential_env=None, max_concurrency=1,
                embedding_dimensions=None)
            for role in ("ocr", "vision")
        })
        self.addAsyncCleanup(close_models)
        calls.clear()  # пробные вызовы при запуске ролей не считаются

    async def test_docx_images_are_described_by_model_gateway(self) -> None:
        result = await converter.convert_document(upload(build_sample_docx(), "doc.docx"), REVISION)

        (image,) = result.images
        self.assertEqual(image.ref.description, "Fixture image")  # OCR пустой: описание только от VLM
        self.assertIn(f"![Fixture image](image://{IMAGE_ID})", result.markdown)
        self.assertEqual(sorted(self.calls), [("describe_image", "image/png"), ("recognize_text", "image/png")])

    @unittest.skipUnless(HAS_PDF_LIBS, "нужны pdfplumber и pypdf")
    async def test_pdf_illustration_is_described_by_model_gateway(self) -> None:
        data = build_sample_pdf(tagged=False, page_content=(
            b"BT /F1 11 Tf 50 700 Td (Picture) Tj ET\nq 20 0 0 20 50 500 cm /Im1 Do Q"))
        result = await converter.convert_document(upload(data, "picture.pdf"), REVISION)
        self.assertEqual(result.images[0].ref.description, "Fixture image")
        self.assertEqual(result.markdown, f"Picture\n\n![Fixture image](image://{IMAGE_ID})\n")
        self.assertEqual(sorted(self.calls), [("describe_image", "image/png"), ("recognize_text", "image/png")])


if __name__ == "__main__":
    unittest.main()
