"""Real atomic files, immutable batches and manifest/image boundary checks."""

from pathlib import Path
import json
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import uuid4, uuid5

from rag.contracts import ImageRef, StepError, StorageError
from rag.ingestion.contracts import ConvertedDocument, DocumentBatch, PreparedChunk, UploadSource
from rag.runtime import _state, upload_spool as spool
from rag.runtime.qdrant import ChunkPoint, TopicPoint


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

    async def test_source_url_survives_manifest_and_batch_round_trip(self):
        url = "https://docs.example/guide?revision=abc&view=1#section"
        source = UploadSource(filename="guide.md", access_level="staff", source_url=url)
        ref = await spool.stage_upload(uuid4(), ConvertedDocument(markdown=self.text, images=[self.image]), source)
        await spool.store_document_batch(ref, DocumentBatch(source=source, chunks=self.batch.chunks))
        self.assertEqual((await spool.open_staged_upload(ref)).source.source_url, url)
        self.assertEqual((await spool.load_document_batch(ref)).source.source_url, url)
        mismatch = DocumentBatch(source=UploadSource(filename="guide.md", access_level="staff",
            source_url="https://docs.example/different"), chunks=self.batch.chunks)
        with self.assertRaisesRegex(StepError, "document_batch_conflict"):
            await spool.store_document_batch(ref, mismatch)

        revision = uuid4()
        ref = await spool.stage_upload(revision, ConvertedDocument(markdown=self.text, images=[self.image]), source)
        await spool.store_document_batch(ref, DocumentBatch(source=source, chunks=self.batch.chunks))
        self.settings.embedding_profile = "embedding-test"
        self.settings.preparation_profile = "preparation-test"
        self.settings.embedding_dimensions = 2
        point = ChunkPoint(chunk_id=uuid5(revision, "0"), document_id=uuid4(), revision_id=revision,
            source_name=source.filename, source_url=url, text=self.text, images=[self.image],
            chunk_no=0, document_version=0, access_level=source.access_level, dense=[0.1, 0.2])
        topic = TopicPoint(point_id=uuid5(revision, "question:0"), revision_id=revision,
            question_no=0, text="Как настроить?", dense=[0.3, 0.4])
        prepared = spool.PreparedRevision(revision_id=revision, preparation_profile="preparation-test",
            embedding_profile="embedding-test", points=[point], topic_points=[topic])
        await spool.store_prepared_revision(ref, prepared)
        self.assertEqual((await spool.load_prepared_revision(ref)).points[0].source_url, url)
        changed = prepared.model_dump()
        changed["points"][0]["source_url"] = "https://docs.example/different"
        with self.assertRaisesRegex(StepError, "prepared_revision_conflict"):
            await spool.store_prepared_revision(ref, spool.PreparedRevision.model_validate(changed))

    async def test_legacy_manifest_and_batch_without_source_url_remain_readable(self):
        await spool.store_document_batch(self.ref, self.batch)
        for path in (self.path, self.path.parent / "manifest.json"):
            data = json.loads(path.read_text(encoding="utf-8"))
            data["source"].pop("source_url")
            path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone((await spool.open_staged_upload(self.ref)).source.source_url)
        self.assertIsNone((await spool.load_document_batch(self.ref)).source.source_url)

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
