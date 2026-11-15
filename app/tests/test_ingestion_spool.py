"""Real atomic files, immutable batches and manifest/image boundary checks."""

from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4

from rag.contracts import ImageRef, StepError, StorageError
from rag.ingestion.contracts import ConvertedDocument, DocumentBatch, PreparedChunk, UploadSource
from rag.runtime import _state, upload_spool as spool


class DocumentBatchStorageTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.settings = SimpleNamespace(spool_dir=Path(self.temp.name), io_timeout_s=2,
            max_markdown_bytes=10000, max_prepared_bytes=100000)
        self.patch = patch.object(_state, "_current", SimpleNamespace(settings=self.settings))
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.revision = uuid4()
        self.source = UploadSource(filename="guide.md", access_level="staff")
        self.image = ImageRef(image_id=uuid4(), description="Diagram")
        self.text = f"Text ![Diagram](image://{self.image.image_id})"
        self.ref = await spool.stage_upload(self.revision,
            ConvertedDocument(markdown=self.text, images=[self.image]), self.source)
        self.batch = DocumentBatch(source=self.source, chunks=[
            PreparedChunk(text=self.text, images=[self.image], locator=None)])
        self.path = Path(self.temp.name) / self.ref / "chunks.json"

    async def test_store_load_and_identical_repeat_survive_new_model_instance(self):
        self.assertIsNone(await spool.load_document_batch(self.ref))
        await spool.store_document_batch(self.ref, self.batch)
        restored = await spool.load_document_batch(self.ref)
        self.assertEqual(restored, self.batch)
        await spool.store_document_batch(self.ref, DocumentBatch.model_validate_json(self.batch.model_dump_json()))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    async def test_different_batch_does_not_replace_existing_bytes(self):
        await spool.store_document_batch(self.ref, self.batch)
        original = self.path.read_bytes()
        self.batch.chunks[0].text += " changed"
        with self.assertRaisesRegex(StepError, "document_batch_conflict"):
            await spool.store_document_batch(self.ref, self.batch)
        self.assertEqual(self.path.read_bytes(), original)

    async def test_missing_or_changed_image_and_access_are_refused(self):
        for batch in (
            DocumentBatch(source=self.source, chunks=[PreparedChunk(text="No image", locator=None)]),
            self.batch.model_copy(update={"source": UploadSource(filename="guide.md", access_level="public")}),
        ):
            with self.assertRaisesRegex(StepError, "document_batch_conflict"):
                await spool.store_document_batch(self.ref, batch)
        self.assertFalse(self.path.exists())

    async def test_corrupt_json_is_not_absence(self):
        self.path.write_text("{broken", encoding="utf-8")
        with self.assertRaises(StorageError):
            await spool.load_document_batch(self.ref)

    async def test_stored_manifest_mismatch_is_storage_error(self):
        await spool.store_document_batch(self.ref, self.batch)
        changed = self.batch.model_copy(update={"source": UploadSource(filename="wrong", access_level="staff")})
        self.path.write_text(changed.model_dump_json(), encoding="utf-8")
        with self.assertRaises(StorageError):
            await spool.load_document_batch(self.ref)

    async def test_failed_atomic_write_does_not_expose_partial_batch(self):
        from rag.runtime import _spool
        with patch.object(_spool.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(StorageError):
                await spool.store_document_batch(self.ref, self.batch)
        self.assertIsNone(await spool.load_document_batch(self.ref))
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])

    async def test_remove_deletes_batch_and_markdown(self):
        await spool.store_document_batch(self.ref, self.batch)
        await spool.remove_staged_upload(self.ref)
        self.assertFalse(self.path.parent.exists())
        self.assertIsNone(await spool.load_document_batch(self.ref))
