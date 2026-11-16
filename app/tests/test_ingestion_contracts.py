"""Трассировка приёма, S3, Markdown, снимка и двух наборов индекса."""

from pathlib import Path
import unittest
from uuid import uuid4, uuid5

from pydantic import ValidationError

from contract_examples import DOCUMENT, IMAGE, MARKDOWN, NOW, REVISION, returned, validate_call_arguments
from rag.contracts import ImageContent, ImageRef
from rag.ingestion.contracts import (
    AcceptedUpload, ClaimedIngestionJob, ConversionResult, ConvertedDocument, ConvertedImage,
    DocumentBatch, IngestionAttempt, JobRef, PreparedChunk, StagedUpload, UploadInput, UploadSource,
)
from rag.ingestion.converter import convert_document
from rag.ingestion.gateway import accept_upload
from rag.ingestion.worker import prepare_chunks
from rag.runtime.postgresql import ingestion as pg
from rag.runtime.qdrant import (
    ChunkPoint, TopicPoint, verify_revision_points, verify_topic_points,
    write_revision_points, write_topic_points,
)
from rag.runtime.upload_spool import (
    PreparedRevision, load_document_batch, load_prepared_revision, open_staged_upload,
    stage_upload, store_document_batch, store_prepared_revision,
)


def prepared_revision(source_url=None):
    """Полный согласованный набор из одного чанка и одного примера темы."""
    chunk = ChunkPoint(chunk_id=uuid5(REVISION, "0"), document_id=DOCUMENT,
                       revision_id=REVISION, source_name="Руководство.docx", source_url=source_url,
                       text=MARKDOWN, images=[IMAGE], chunk_no=0, document_version=0,
                       access_level="employees", dense=[0.1, 0.2])
    topic = TopicPoint(point_id=uuid5(REVISION, "question:0"), revision_id=REVISION,
                       question_no=0, text="Где фильтр?", dense=[0.3, 0.4])
    return PreparedRevision(revision_id=REVISION, preparation_profile="ingestion-v1",
                            embedding_profile="embedding-v1", points=[chunk], topic_points=[topic])


class IngestionContractTests(unittest.TestCase):
    def test_document_to_two_indexes_trace(self):
        async def stream():
            yield b"document"

        upload = UploadInput(content=stream(), source=UploadSource(filename="Руководство.docx", access_level="employees",
                             source_url="https://docs.example/manual?revision=abc"),
                             document_id=None)
        validate_call_arguments(accept_upload, upload)
        validate_call_arguments(convert_document, upload, REVISION)
        from rag.runtime.s3 import put_image
        converted = returned(convert_document, ConversionResult(markdown=MARKDOWN, images=[
            ConvertedImage(ref=IMAGE, content=ImageContent(data=b"png", mime_type="image/png"))]))
        for image in converted.images:
            validate_call_arguments(put_image, image.ref.image_id, image.content)
        ready = ConvertedDocument(markdown=converted.markdown, images=[image.ref for image in converted.images])
        validate_call_arguments(stage_upload, REVISION, ready, upload.source)
        staging_ref = returned(stage_upload, f"uploads/{REVISION}")
        # Gateway формирует и сохраняет полную партию до регистрации/outbox.
        staged = returned(open_staged_upload, StagedUpload(
            path=Path("uploads/converted.md"), source=upload.source, images=ready.images))
        validate_call_arguments(prepare_chunks, staged)
        chunks = returned(prepare_chunks, [PreparedChunk(text=ready.markdown, locator=None, images=ready.images)])
        batch = DocumentBatch(source=upload.source, chunks=chunks)
        validate_call_arguments(store_document_batch, staging_ref, batch)
        validate_call_arguments(pg.register_revision, REVISION, upload.document_id,
                                staging_ref, upload.source, [IMAGE.image_id])
        accepted = returned(pg.register_revision, AcceptedUpload(
            kind="accepted", document_id=DOCUMENT, job_id=REVISION))
        event = JobRef.model_validate_json(JobRef(job_id=accepted.job_id).model_dump_json())
        validate_call_arguments(pg.claim_ingestion_job, event.job_id)
        job = returned(pg.claim_ingestion_job, ClaimedIngestionJob(
            attempt=IngestionAttempt(job_id=event.job_id, attempt_no=1, deadline_at=NOW),
            document_id=accepted.document_id, staging_ref=staging_ref))
        validate_call_arguments(load_document_batch, job.staging_ref)
        restored_batch = returned(load_document_batch, DocumentBatch.model_validate_json(batch.model_dump_json()))
        self.assertEqual(restored_batch, batch)
        validate_call_arguments(pg.read_document_version, restored_batch.source.filename)
        current = returned(pg.read_document_version, None)
        validate_call_arguments(pg.reserve_document_version, job.attempt, current)
        reservation = returned(pg.reserve_document_version, pg.ReservedRevision(
            revision_id=REVISION, document_id=DOCUMENT, document_version=0, base_version=None))
        snapshot = prepared_revision(source_url=upload.source.source_url)
        self.assertEqual(snapshot.points[0].text, chunks[0].text)
        self.assertEqual(snapshot.points[0].document_version, reservation.document_version)
        self.assertEqual(snapshot.points[0].access_level, batch.source.access_level)
        validate_call_arguments(store_prepared_revision, job.staging_ref, snapshot)
        restored = PreparedRevision.model_validate_json(snapshot.model_dump_json())
        self.assertEqual(returned(load_prepared_revision, restored), snapshot)
        validate_call_arguments(pg.begin_revision_write, job.attempt,
                                len(restored.points), len(restored.topic_points))
        validate_call_arguments(write_revision_points, event.job_id, restored.points)
        validate_call_arguments(write_topic_points, event.job_id, restored.topic_points)
        validate_call_arguments(verify_revision_points, event.job_id, len(restored.points))
        validate_call_arguments(verify_topic_points, event.job_id, len(restored.topic_points))
        validate_call_arguments(pg.publish_revision, job.attempt)
        self.assertEqual(returned(pg.publish_revision, 42), 42)
        self.assertEqual(restored.points[0].source_url, upload.source.source_url)

    def test_existing_document_version_zero_is_not_absence(self):
        current = returned(pg.read_document_version, pg.DocumentVersion(
            document_id=DOCUMENT, document_version=0))
        attempt = IngestionAttempt(job_id=REVISION, attempt_no=1, deadline_at=NOW)
        validate_call_arguments(pg.reserve_document_version, attempt, current.document_version)
        reservation = returned(pg.reserve_document_version, pg.ReservedRevision(
            revision_id=REVISION, document_id=DOCUMENT, document_version=1, base_version=0))
        resumed = ClaimedIngestionJob(attempt=attempt, document_id=DOCUMENT,
            staging_ref="uploads/revision", document_version=reservation.document_version,
            base_version=reservation.base_version, expected_chunk_count=1, expected_topic_count=1)
        validate_call_arguments(pg.reserve_document_version, resumed.attempt, resumed.base_version)
        # Трассировка формы повтора: он несёт закреплённую версию, а не запрашивает следующую.
        self.assertEqual(returned(pg.reserve_document_version, reservation).document_version, resumed.document_version)

    def test_batch_and_version_boundaries(self):
        source = UploadSource(filename="Руководство.docx", access_level="employees")
        with self.assertRaises(ValidationError):
            DocumentBatch(source=source, chunks=[])
        with self.assertRaises(ValidationError):
            UploadSource(filename="Руководство.docx", access_level=" ")
        with self.assertRaises(ValidationError):
            UploadSource(filename="Руководство.docx")
        first = PreparedChunk(text=MARKDOWN, locator=None, images=[IMAGE])
        other = PreparedChunk(text=MARKDOWN, locator=None,
            images=[ImageRef(image_id=IMAGE.image_id, description="Другое описание")])
        with self.assertRaises(ValidationError):
            DocumentBatch(source=source, chunks=[first, other])
        DocumentBatch(source=source, chunks=[first, first])  # Перекрытие может повторять изображение.
        for version, base in ((1, None), (0, 0), (2, 0), (-1, None)):
            with self.subTest(version=version, base=base), self.assertRaises(ValidationError):
                pg.ReservedRevision(revision_id=REVISION, document_id=DOCUMENT,
                    document_version=version, base_version=base)
        with self.assertRaises(ValidationError):
            ClaimedIngestionJob(attempt=IngestionAttempt(job_id=REVISION, attempt_no=1, deadline_at=NOW),
                document_id=DOCUMENT, staging_ref="uploads/revision", base_version=0)

    def test_snapshot_keeps_one_document_version_and_access_level(self):
        original = prepared_revision()
        for field, value in (("document_version", 1), ("access_level", "operators"),
                             ("source_name", "Другое имя"), ("source_url", "https://docs.example/other")):
            data = original.model_dump()
            second = dict(data["points"][0], chunk_no=1, chunk_id=uuid5(REVISION, "1"))
            second[field] = value
            data["points"].append(second)
            with self.subTest(field=field), self.assertRaises(ValidationError):
                PreparedRevision.model_validate(data)

    def test_snapshot_rejects_corruption_and_wrong_identity(self):
        original = prepared_revision()
        changes = [
            lambda data: data["points"][0].update(chunk_id=uuid4()),
            lambda data: data["points"][0].update(revision_id=uuid4()),
            lambda data: data["topic_points"][0].update(point_id=uuid4()),
            lambda data: data["topic_points"][0].update(question_no=1),
            lambda data: data["topic_points"][0].update(dense=[0.1]),
            lambda data: data["points"][0].update(dense=[float("nan"), 0.1]),
            lambda data: data.update(points=[]),
        ]
        for change in changes:
            data = original.model_dump()
            change(data)
            with self.subTest(change=changes.index(change)), self.assertRaises(ValidationError):
                PreparedRevision.model_validate(data)

    def test_expected_counts_are_both_present_or_both_absent(self):
        with self.assertRaises(ValidationError):
            ClaimedIngestionJob(attempt=IngestionAttempt(job_id=REVISION, attempt_no=2, deadline_at=NOW),
                                document_id=DOCUMENT, staging_ref="uploads/revision", expected_chunk_count=1)


if __name__ == "__main__":
    unittest.main()
