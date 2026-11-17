"""Upload admission, persistence order, authentication and uncertain commits."""

import asyncio
from contextlib import ExitStack, asynccontextmanager
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from urllib.parse import quote
from uuid import uuid4

import httpx
from pydantic import SecretStr

from rag.contracts import ImageContent, ImageRef, StepError, StorageError
from rag.ingestion import application, gateway
from rag.ingestion._gateway_settings import _GatewaySettings
from rag.ingestion.contracts import (
    AcceptedUpload, ConversionResult, ConvertedImage, PreparedChunk, StagedUpload,
    UploadInput, UploadRejected, UploadSource,
)


@asynccontextmanager
async def _test_upload_lease(revision_id):
    yield


class GatewayTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(gateway, "_protect_upload", _test_upload_lease))
        self.addCleanup(self.stack.close)
        self.settings = _GatewaySettings(_env_file=None, token="test-secret", max_upload_bytes=10)
        self.stack.enter_context(patch.object(gateway, "_GatewaySettings", return_value=self.settings))
        self.events = self.stack.enter_context(patch.object(gateway, "emit_event"))
        self.revision = uuid4()
        self.document = uuid4()
        self.image = ImageRef(image_id=uuid4(), description="Diagram")
        self.source = UploadSource(filename="manual.pdf", access_level="employees")
        self.markdown = f"Text ![Diagram](image://{self.image.image_id})"
        self.converted = ConversionResult(markdown=self.markdown, images=[ConvertedImage(
            ref=self.image, content=ImageContent(data=b"image", mime_type="image/png"),
        )])
        self.staged = StagedUpload(path=Path("converted.md"), source=self.source, images=[self.image])
        self.stack.enter_context(patch.object(gateway, "uuid4", return_value=self.revision))
        self.calls = Mock()
        for name in (
            "convert_document", "put_image", "stage_upload", "open_staged_upload",
            "prepare_chunks", "store_document_batch", "register_revision", "remove_staged_upload",
        ):
            mock = self.stack.enter_context(patch.object(gateway, name, new_callable=AsyncMock))
            setattr(self, name, mock)
            self.calls.attach_mock(mock, name)
        self.convert_document.side_effect = self._convert
        self.stage_upload.return_value = f"uploads/{self.revision}"
        self.open_staged_upload.return_value = self.staged
        self.prepare_chunks.return_value = [PreparedChunk(
            text=self.markdown, locator=None, images=[self.image],
        )]
        self.accepted = AcceptedUpload(kind="accepted", job_id=self.revision, document_id=self.document)
        self.register_revision.return_value = self.accepted
        self.load_document_batch = self.stack.enter_context(patch.object(
            gateway, "load_document_batch", new=AsyncMock(return_value=None)))

    async def _convert(self, upload, revision):
        self.received = b"".join([part async for part in upload.content])
        return self.converted

    def _input(self, parts=(b"document",), document_id=None):
        async def stream():
            for part in parts:
                yield part
        return UploadInput(content=stream(), source=self.source, document_id=document_id)

    async def test_accepts_only_after_images_markdown_batch_and_registration(self):
        result = await gateway.accept_upload(self._input(document_id=self.document))
        self.assertEqual(result, self.accepted)
        self.assertEqual(self.received, b"document")
        self.assertEqual([call[0] for call in self.calls.mock_calls], [
            "convert_document", "put_image", "stage_upload", "open_staged_upload",
            "prepare_chunks", "store_document_batch", "register_revision",
        ])
        batch = self.store_document_batch.await_args.args[1]
        self.assertEqual(batch.source, self.source)
        self.assertEqual(batch.chunks, self.prepare_chunks.return_value)
        self.register_revision.assert_awaited_once_with(
            self.revision, self.document, f"uploads/{self.revision}", self.source, [self.image.image_id],
        )

    async def test_source_url_reaches_manifest_batch_and_registration(self):
        url = "https://docs.example/manual.pdf?revision=abc&view=1#page=2"
        self.source = UploadSource(filename="manual.pdf", access_level="employees", source_url=url)
        self.staged.source = self.source
        self.assertEqual(await gateway.accept_upload(self._input()), self.accepted)
        self.assertEqual(self.convert_document.await_args.args[0].source.source_url, url)
        self.assertEqual(self.stage_upload.await_args.args[2].source_url, url)
        self.assertEqual(self.store_document_batch.await_args.args[1].source.source_url, url)
        self.assertEqual(self.register_revision.await_args.args[3].source_url, url)

    async def test_empty_and_oversized_streams_are_rejected_before_storage(self):
        for parts, code in (((), "invalid_upload"), ((b"12345", b"678901"), "too_large")):
            with self.subTest(code=code):
                result = await gateway.accept_upload(self._input(parts))
                self.assertEqual(result.code, code)
                self.stage_upload.assert_not_awaited()
                self.register_revision.assert_not_awaited()

    async def test_upload_lease_covers_conversion_and_unknown_registration(self):
        active = False
        @asynccontextmanager
        async def lease(revision_id):
            nonlocal active
            self.assertEqual(revision_id, self.revision)
            active = True
            try:
                yield
            finally:
                active = False
        async def convert(upload, revision):
            self.assertTrue(active)
            return await self._convert(upload, revision)
        async def register(*args):
            self.assertTrue(active)
            raise StorageError("unknown commit")
        self.convert_document.side_effect = convert
        self.register_revision.side_effect = register
        with patch.object(gateway, "_protect_upload", lease), self.assertRaises(StorageError):
            await gateway.accept_upload(self._input())
        self.assertFalse(active)
        self.remove_staged_upload.assert_not_awaited()
        finished = self.events.call_args.kwargs
        self.assertEqual(finished["error_code"], "storage_unconfirmed")
        registration = next(step for step in finished["steps"] if step["name"] == "register_revision")
        self.assertEqual(registration["outcome"], "failed")

    async def test_stage_timings_are_measured_without_content(self):
        from rag.runtime.events import _context, _event_context

        async def prepare(staged):
            self.assertEqual(_context.get()["job_id"], str(self.revision))
            self.assertEqual(_context.get()["route"], "http.upload")
            return [PreparedChunk(text=self.markdown, locator=None, images=[self.image])]

        self.prepare_chunks.side_effect = prepare
        with _event_context(route="http.upload"):
            await gateway.accept_upload(self._input())
        finished = self.events.call_args.kwargs
        self.assertGreaterEqual(finished["duration_ms"], 0)
        steps = finished["steps"]
        self.assertEqual([step["name"] for step in steps], [
            "convert_document", "put_image", "stage_upload", "open_staged_upload",
            "prepare_chunks", "store_document_batch", "register_revision",
        ])
        for step in steps:
            self.assertEqual(step["outcome"], "completed")
            self.assertGreaterEqual(step["duration_ms"], 0)
            self.assertLessEqual(step["offset_ms"] + step["duration_ms"],
                                 finished["duration_ms"] + 0.01)
        self.assertNotIn(self.markdown, str(finished))
        self.assertNotIn(self.source.filename, str(finished))

    async def test_converter_business_rejection_and_model_failure_are_distinct(self):
        self.convert_document.side_effect = StepError("unsupported_format", False)
        self.assertEqual((await gateway.accept_upload(self._input())).code, "unsupported_format")
        self.convert_document.side_effect = StepError("model_unavailable", True)
        with self.assertRaises(StepError):
            await gateway.accept_upload(self._input())
        self.register_revision.assert_not_awaited()

    async def test_s3_failure_does_not_register(self):
        self.put_image.side_effect = StepError("s3_unavailable", True)
        with self.assertRaises(StepError):
            await gateway.accept_upload(self._input())
        self.stage_upload.assert_not_awaited()
        self.register_revision.assert_not_awaited()

    async def test_indivisible_chunk_rejection_is_permanent_without_registration(self):
        self.prepare_chunks.side_effect = StepError("chunk_too_large", False)
        result = await gateway.accept_upload(self._input())
        self.assertEqual(result, UploadRejected(kind="rejected", code="conversion_failed"))
        self.register_revision.assert_not_awaited()
        self.store_document_batch.assert_not_awaited()
        self.remove_staged_upload.assert_awaited_once_with(f'uploads/{self.revision}')
        finished = self.events.call_args.kwargs
        self.assertEqual(finished["outcome"], "conversion_failed")
        steps = {step["name"]: step for step in finished["steps"]}
        self.assertEqual(steps["prepare_chunks"]["error_code"], "chunk_too_large")
        self.assertEqual(steps["remove_staged_upload"]["outcome"], "completed")
        self.prepare_chunks.side_effect = StepError("chunk_too_large", True)
        with self.assertRaises(StepError):
            await gateway.accept_upload(self._input())

    async def test_real_chunking_failure_returns_422_through_http(self):
        from rag.ingestion.worker import prepare_chunks

        self.converted.markdown = 'x' * 2100 + '\n\n' + self.markdown
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'converted.md'
            path.write_text(self.converted.markdown, encoding='utf-8')
            self.open_staged_upload.return_value = self.staged.model_copy(update={'path': path})
            self.prepare_chunks.side_effect = prepare_chunks
            application.app.state.ingestion_settings = self.settings
            try:
                with patch.dict('os.environ', INGESTION_CHUNK_CHARACTERS='2000',
                                INGESTION_CHUNK_OVERLAP='200',
                                INGESTION_EMBEDDING_INPUT_CHARACTERS='4000'):
                    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app),
                                                 base_url='http://test') as client:
                        response = await client.post(
                            '/admin/ingestion/upload?filename=manual.pdf&access_level=employees',
                            content=b'document', headers={'Authorization': 'Bearer test-secret',
                                                          'Content-Type': 'application/pdf'})
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json(), {'kind': 'rejected', 'code': 'conversion_failed'})
                self.register_revision.assert_not_awaited()
            finally:
                del application.app.state.ingestion_settings


    async def test_non_utf8_image_bytes_reach_s3_without_json_encoding(self):
        self.converted.images[0].content.data = b"\x89PNG\r\n\x1a\n\xff\x00"
        result = await gateway.accept_upload(self._input())
        self.assertEqual(result, self.accepted)
        self.assertEqual(self.put_image.await_args.args[1].data, self.converted.images[0].content.data)

    async def test_unknown_document_removes_only_staging(self):
        self.register_revision.return_value = None
        result = await gateway.accept_upload(self._input(document_id=self.document))
        self.assertEqual(result.code, "unknown_document")
        self.remove_staged_upload.assert_awaited_once_with(f"uploads/{self.revision}")

    async def test_unknown_commit_preserves_staging(self):
        self.register_revision.side_effect = StorageError("unknown commit")
        with self.assertRaises(StorageError):
            await gateway.accept_upload(self._input())
        self.remove_staged_upload.assert_not_awaited()

    async def test_unknown_batch_write_is_confirmed_before_registration(self):
        self.store_document_batch.side_effect = StorageError("unknown write")
        from rag.ingestion.contracts import DocumentBatch
        self.load_document_batch.return_value = DocumentBatch(
            source=self.source, chunks=self.prepare_chunks.return_value)
        self.assertEqual(await gateway.accept_upload(self._input()), self.accepted)
        self.load_document_batch.assert_awaited_once()
        self.register_revision.assert_awaited_once()

    async def test_registration_timeout_preserves_staging(self):
        self.settings.upload_timeout_s = 0.01
        async def wait_forever(*args):
            await asyncio.Event().wait()
        self.register_revision.side_effect = wait_forever
        with self.assertRaises(StorageError):
            await gateway.accept_upload(self._input())
        self.remove_staged_upload.assert_not_awaited()

    async def test_registration_cancellation_preserves_unknown_commit_materials(self):
        entered = asyncio.Event()
        async def register(*args):
            entered.set()
            await asyncio.Event().wait()
        self.register_revision.side_effect = register
        task = asyncio.create_task(gateway.accept_upload(self._input()))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.remove_staged_upload.assert_not_awaited()

    async def test_conversion_timeout_cancels_work_and_does_not_register(self):
        self.settings.upload_timeout_s = 0.01
        stopped = False
        async def convert(*args):
            nonlocal stopped
            try:
                await asyncio.Event().wait()
            finally:
                stopped = True
        self.convert_document.side_effect = convert
        with self.assertRaises(StepError) as caught:
            await gateway.accept_upload(self._input())
        self.assertEqual(caught.exception.code, "upload_timeout")
        self.assertTrue(stopped)
        self.register_revision.assert_not_awaited()

    async def test_model_scope_and_logs_use_job_id_without_original(self):
        from rag.runtime.events import _context
        async def convert(upload, revision):
            self.assertEqual(_context.get()["job_id"], str(self.revision))
            return await self._convert(upload, revision)
        self.convert_document.side_effect = convert
        await gateway.accept_upload(self._input())
        for call in self.events.call_args_list:
            self.assertEqual(call.kwargs["job_id"], str(self.revision))
            self.assertNotIn("manual.pdf", str(call))
            self.assertNotIn("test-secret", str(call))

    async def test_empty_chunks_or_batch_failure_never_register(self):
        self.prepare_chunks.return_value = []
        self.assertEqual((await gateway.accept_upload(self._input())).code, "conversion_failed")
        self.register_revision.assert_not_awaited()
        self.prepare_chunks.return_value = [PreparedChunk(text="text", locator=None)]
        self.store_document_batch.side_effect = StorageError("disk failed")
        with self.assertRaises(StorageError):
            await gateway.accept_upload(self._input())
        self.register_revision.assert_not_awaited()


class HttpUploadTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = _GatewaySettings(_env_file=None, token="test-secret", max_upload_bytes=10)
        application.app.state.ingestion_settings = self.settings
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application.app), base_url="http://test",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.enterContext(patch.object(gateway, "_protect_upload", _test_upload_lease))
        self.mock = AsyncMock(return_value=AcceptedUpload(
            kind="accepted", job_id=uuid4(), document_id=uuid4(),
        ))
        self.patch = patch.object(application, "accept_upload", self.mock)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(lambda: delattr(application.app.state, "ingestion_settings"))
        self.url = "/admin/ingestion/upload?filename=manual.pdf&access_level=employees"
        self.headers = {"Authorization": "Bearer test-secret", "Content-Type": "application/pdf"}

    async def test_http_acceptance_passes_stream_and_metadata(self):
        document_id = uuid4()
        source_url = "https://docs.example/manual.pdf?revision=abc&view=1#page=2"
        async def accept(upload):
            self.assertEqual(upload.document_id, document_id)
            self.assertEqual(upload.source.filename, "manual.pdf")
            self.assertEqual(upload.source.access_level, "employees")
            self.assertEqual(upload.source.source_url, source_url)
            self.assertEqual(b"".join([part async for part in upload.content]), b"pdf")
            return self.mock.return_value
        self.mock.side_effect = accept
        response = await self.client.post(self.url + f"&document_id={document_id}&source_url={quote(source_url, safe='')}",
                                          headers=self.headers, content=b"pdf")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json()["kind"], "accepted")
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_markdown_upload_preserves_bytes_and_source_name(self):
        async def accept(upload):
            self.assertEqual(upload.source.filename, "manual.md")
            self.assertIsNone(upload.source.source_url)
            self.assertEqual(b"".join([part async for part in upload.content]), b"# Manual")
            return self.mock.return_value
        self.mock.side_effect = accept
        response = await self.client.post(self.url.replace("manual.pdf", "manual.md"),
            headers={"Authorization": "Bearer test-secret", "Content-Type": "text/markdown"},
            content=b"# Manual")
        self.assertEqual(response.status_code, 202)

    async def test_invalid_source_url_is_rejected_before_gateway(self):
        for url in ("", "file:///tmp/manual.pdf", "javascript:alert(1)", "https://user:password@docs.example"):
            with self.subTest(url=url):
                response = await self.client.post(self.url + "&source_url=" + quote(url, safe=""),
                                                  headers=self.headers, content=b"pdf")
                self.assertEqual(response.status_code, 422)
                self.assertEqual(response.json()["detail"], "invalid_upload_metadata")
        self.mock.assert_not_awaited()

    async def test_authentication_runs_before_body_and_metadata(self):
        for authorization in (None, "Bearer wrong", "Basic test-secret", "Bearer "):
            read = False
            async def body():
                nonlocal read
                read = True
                yield b"pdf"
            headers = {} if authorization is None else {"Authorization": authorization}
            response = await self.client.post("/admin/ingestion/upload", headers=headers, content=body())
            self.assertEqual(response.status_code, 401)
            self.assertEqual(response.headers["www-authenticate"], "Bearer")
            self.assertFalse(read)
        self.mock.assert_not_awaited()

    async def test_missing_token_fails_closed(self):
        self.settings.token = SecretStr("")
        response = await self.client.post(self.url, headers=self.headers, content=b"pdf")
        self.assertEqual(response.status_code, 503)
        self.mock.assert_not_awaited()

    async def test_metadata_content_type_size_and_empty_body(self):
        cases = [
            ("/admin/ingestion/upload?filename=manual.pdf", self.headers, b"pdf", 422),
            (self.url + "&document_id=bad", self.headers, b"pdf", 422),
            (self.url.replace("manual.pdf", "manual.txt"), self.headers, b"txt", 415),
            (self.url, dict(self.headers, **{"Content-Type": "multipart/form-data"}), b"pdf", 415),
            (self.url, self.headers, b"12345678901", 413),
            (self.url, self.headers, b"", 400),
            (self.url, dict(self.headers, **{"Content-Length": "-1"}), b"pdf", 400),
        ]
        for url, headers, content, status in cases:
            with self.subTest(status=status, url=url):
                response = await self.client.post(url, headers=headers, content=content)
                self.assertEqual(response.status_code, status)
        self.mock.assert_not_awaited()

    async def test_rejection_mapping_and_sanitized_technical_errors(self):
        for code, status in (
            ("unknown_document", 404), ("invalid_upload", 400), ("too_large", 413),
            ("unsupported_format", 415), ("conversion_failed", 422),
        ):
            self.mock.return_value = UploadRejected(kind="rejected", code=code)
            response = await self.client.post(self.url, headers=self.headers, content=b"pdf")
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json(), {"kind": "rejected", "code": code})
        for failure in (StorageError("secret"), StepError("secret", True), NotImplementedError()):
            self.mock.side_effect = failure
            response = await self.client.post(self.url, headers=self.headers, content=b"pdf")
            self.assertEqual(response.status_code, 503)
            self.assertEqual(response.json(), {"detail": "ingestion_unavailable"})

    async def test_chunked_upload_is_limited_by_gateway(self):
        # No Content-Length: exercise the actual Gateway stream guard through HTTP.
        self.patch.stop()
        self.mock.side_effect = None
        async def convert(upload, revision):
            async for _ in upload.content:
                pass
            self.fail("Oversized body reached end of conversion")
        async def body():
            yield b"123456"
            yield b"78901"
        with patch.object(gateway, "_GatewaySettings", return_value=self.settings), \
             patch.object(gateway, "convert_document", side_effect=convert), \
             patch.object(gateway, "emit_event"):
            response = await self.client.post(self.url, headers=self.headers, content=body())
        self.assertEqual(response.status_code, 413)

    async def test_no_job_status_endpoint(self):
        response = await self.client.get(f"/admin/ingestion/jobs/{uuid4()}", headers=self.headers)
        self.assertEqual(response.status_code, 404)


class ConfigurationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.object(application, '_sweep_orphans', AsyncMock(return_value=0)))
    async def test_lifespan_opens_and_closes_runtime_without_unused_models(self):
        settings = _GatewaySettings(_env_file=None, token="test-secret")
        with patch.object(application, "_GatewaySettings", return_value=settings), \
             patch.object(application, "RuntimeSettings") as runtime, \
             patch.object(application, "start_runtime", new_callable=AsyncMock) as start, \
             patch.object(application, "close_runtime", new_callable=AsyncMock) as close, \
             patch.object(application, "start_models", new_callable=AsyncMock) as models, \
             patch.object(application, "close_models", new_callable=AsyncMock):
            runtime.return_value.model_bindings = SecretStr("{}")
            async with application._lifespan(application.app):
                self.assertIs(application.app.state.ingestion_settings, settings)
                start.assert_awaited_once()
                models.assert_not_awaited()
            close.assert_awaited_once()
            self.assertIsNone(application.app.state.ingestion_settings)

    async def test_lifespan_closes_partial_model_startup_and_coordination(self):
        settings = _GatewaySettings(_env_file=None, token="test-secret")
        with patch.object(application, "_GatewaySettings", return_value=settings), \
             patch.object(application, "RuntimeSettings") as runtime, \
             patch.object(application, "TypeAdapter") as adapter, \
             patch.object(application, "start_runtime", new_callable=AsyncMock), \
             patch.object(application, "close_runtime", new_callable=AsyncMock) as close, \
             patch.object(application, "start_models", new_callable=AsyncMock) as models, \
             patch.object(application, "close_models", new_callable=AsyncMock) as stop_models, \
             patch.object(application, "configure_coordination") as coordination:
            runtime.return_value.startup_timeout_s = 1
            adapter.return_value.validate_json.return_value = {"ocr": object(), "embedding": object()}
            models.side_effect = StepError("model_unavailable", True)
            with self.assertRaises(StepError):
                async with application._lifespan(application.app):
                    self.fail("Started after model startup failure")
            self.assertEqual(set(models.await_args.args[0]), {"ocr"})
            stop_models.assert_awaited_once()
            close.assert_awaited_once()
            self.assertIsNone(coordination.call_args_list[-1].args[0])

    async def test_lifespan_fails_before_runtime_for_empty_token(self):
        with patch.object(application, "_GatewaySettings", return_value=_GatewaySettings(
            _env_file=None, token="",
        )), patch.object(application, "start_runtime", new_callable=AsyncMock) as start:
            with self.assertRaisesRegex(RuntimeError, "RAG_INGESTION_TOKEN"):
                async with application._lifespan(application.app):
                    self.fail("Started without a token")
            start.assert_not_awaited()

    async def test_reads_dotenv_without_exposing_token_in_repr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env"
            path.write_text("RAG_INGESTION_TOKEN=dotenv-secret\nRAG_INGESTION_MAX_UPLOAD_BYTES=42\n",
                            encoding="utf-8")
            with patch.dict("os.environ", {}, clear=True):
                settings = _GatewaySettings(_env_file=path)
                self.assertEqual(settings.token.get_secret_value(), "dotenv-secret")
                self.assertEqual(settings.max_upload_bytes, 42)
                self.assertNotIn("dotenv-secret", repr(settings))
                with patch.dict("os.environ", {"RAG_INGESTION_TOKEN": "environment-secret"}):
                    self.assertEqual(_GatewaySettings(_env_file=path).token.get_secret_value(),
                                     "environment-secret")
