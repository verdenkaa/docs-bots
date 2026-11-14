"""Ingestion Gateway: accept_upload проводит документ от потока до зарегистрированного задания."""

import contextlib
import io
import unittest
from unittest import mock
from uuid import UUID, uuid4

from sample_docx import build_sample_docx

from rag.contracts import StepError, StorageError
from rag.ingestion import converter, gateway
from rag.ingestion._config import ConverterSettings
from rag.ingestion.contracts import AcceptedUpload, UploadInput, UploadSource
from rag.model_gateway.contracts import ImageDescription, OcrResult

DOCUMENT = UUID("3f0c1f0e-6f0a-4c59-a3a3-0a4a55b0c001")


def upload(data: bytes, filename: str, document_id: UUID | None = None) -> UploadInput:
    async def stream():
        yield data

    return UploadInput(content=stream(), source=UploadSource(filename=filename), document_id=document_id)


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.calls: list[str] = []

        async def put(image_id, content):
            self.calls.append("put_image")

        async def stage(revision_id, document, source):
            self.calls.append("stage_upload")
            self.staged = (revision_id, document, source)
            return f"uploads/{revision_id}"

        async def register(revision_id, document_id, staging_ref, source_name, image_ids):
            self.calls.append("register_revision")
            self.registered = (revision_id, document_id, staging_ref, source_name, list(image_ids))
            return AcceptedUpload(kind="accepted", job_id=revision_id, document_id=document_id or DOCUMENT)

        self.put_image = self.patch(gateway, "put_image", mock.AsyncMock(side_effect=put))
        self.patch(gateway, "stage_upload", mock.AsyncMock(side_effect=stage))
        self.register_revision = self.patch(gateway, "register_revision", mock.AsyncMock(side_effect=register))
        self.remove_staged_upload = self.patch(gateway, "remove_staged_upload", mock.AsyncMock())
        self.patch(converter.model_gateway, "describe_image",
                   mock.AsyncMock(return_value=ImageDescription(profile_id="vision", description="Кнопка")))
        self.patch(converter.model_gateway, "recognize_text",
                   mock.AsyncMock(return_value=OcrResult(profile_id="ocr", text="")))
        self.patch(converter, "converter_settings", mock.Mock(return_value=ConverterSettings(_env_file=None)))
        stdout = contextlib.redirect_stdout(io.StringIO())  # события логов
        stdout.__enter__()
        self.addCleanup(stdout.__exit__, None, None, None)

    def patch(self, target, name: str, fake):
        patcher = mock.patch.object(target, name, fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        return fake

    async def test_docx_is_converted_stored_and_registered(self) -> None:
        result = await gateway.accept_upload(upload(build_sample_docx(), "Руководство.docx"))

        self.assertEqual((result.kind, result.document_id), ("accepted", DOCUMENT))
        # картинки подтверждены в S3 до записи Markdown, Markdown — до регистрации задания
        self.assertEqual(self.calls, ["put_image", "stage_upload", "register_revision"])
        revision_id, document, source = self.staged
        self.assertEqual(revision_id, result.job_id)
        self.assertEqual(source.filename, "Руководство.docx")
        (image,) = document.images
        self.assertIn(f"](image://{image.image_id})", document.markdown)
        image_id, content = self.put_image.await_args.args
        self.assertEqual((image_id, content.mime_type), (image.image_id, "image/png"))
        self.assertEqual(self.registered,
                         (revision_id, None, f"uploads/{revision_id}", "Руководство.docx", [image.image_id]))

    async def test_markdown_passes_through_unchanged(self) -> None:
        text = "# Заголовок\n\nФильтр `!> 200` не работает.\n"
        result = await gateway.accept_upload(upload(text.encode(), "notes.md", DOCUMENT))

        self.assertEqual((result.kind, result.document_id), ("accepted", DOCUMENT))
        self.assertEqual(self.staged[1].markdown, text)
        self.put_image.assert_not_awaited()
        self.assertEqual(self.registered[1], DOCUMENT)

    async def test_final_converter_refusals_become_rejected(self) -> None:
        cases = [(b"", "empty.md", "invalid_upload"), (b"plain", "notes.txt", "unsupported_format"),
                 (b"PK\x03\x04 not a zip", "doc.docx", "conversion_failed")]
        for data, filename, code in cases:
            with self.subTest(code=code):
                result = await gateway.accept_upload(upload(data, filename))
                self.assertEqual((result.kind, result.code), ("rejected", code))
        self.assertEqual(self.calls, [])

    async def test_unknown_document_removes_staged_markdown(self) -> None:
        self.register_revision.side_effect = None
        self.register_revision.return_value = None
        result = await gateway.accept_upload(upload(b"# text\n", "notes.md", uuid4()))

        self.assertEqual((result.kind, result.code), ("rejected", "unknown_document"))
        self.remove_staged_upload.assert_awaited_once_with(f"uploads/{self.staged[0]}")

    async def test_technical_failures_are_not_rejections(self) -> None:
        failing = mock.AsyncMock(side_effect=StepError("model_unavailable", True))
        with mock.patch.object(converter.model_gateway, "describe_image", failing):
            with self.assertRaises(StepError):
                await gateway.accept_upload(upload(build_sample_docx(), "doc.docx"))
        self.assertEqual(self.calls, [])

        self.register_revision.side_effect = StorageError("commit unknown")
        with self.assertRaises(StorageError):
            await gateway.accept_upload(upload(b"# text\n", "notes.md"))
        # исход коммита неизвестен: Markdown остаётся до сверки
        self.remove_staged_upload.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
