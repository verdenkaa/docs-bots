"""Real PostgreSQL, Redis, RabbitMQ, Qdrant and S3; opt in with the isolated Compose stack."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from uuid import uuid4, uuid5

import aio_pika
from sqlalchemy import text

from rag.contracts import (
    AnsweredOutcome,
    DeliveryRef,
    ImageContent,
    NoticeOutcome,
    RagResult,
    SourceRef,
    StepError,
    StorageError,
    SupportOutcome,
    TextQuestion,
)
from rag.ingestion.contracts import ConvertedDocument, UploadSource
from rag.rag_api.contracts import (
    AccessDenied,
    AccessRecord,
    Conflict,
    NewQuestion,
    Principal,
    SupportAction,
    SupportCommand,
    TextPart,
)
from rag.runtime import _state, qdrant, rabbitmq, redis, s3, upload_spool
from rag.runtime._http import _s3
from rag.runtime.lifecycle import (
    migrate_runtime,
    start_runtime,
    close_runtime,
    prepare_runtime_storage,
)
from rag.runtime.media import load_source_metadata
from rag.runtime.postgresql import cleanup, ingestion, outbox, rag_api, rag_core, support
from rag.runtime.postgresql._db import _transaction
from rag.runtime.queue_relay import relay_outbox_batch


def now():
    return datetime.now(UTC)


async def _execute(connection, sql, **params):
    # Independent SQL fixtures inspect committed database state without relying
    # on the ORM mappings that these tests are verifying.
    return await connection.execute(text(sql), params)


async def _one(connection, sql, **params):
    return (await _execute(connection, sql, **params)).mappings().first()


def answer():
    revision = uuid4()
    return RagResult(
        answer_text="A documented answer",
        sources=[
            SourceRef(
                chunk_id=uuid5(revision, "0"),
                document_id=uuid4(),
                revision_id=revision,
                source_name="Guide",
            )
        ],
    )


@unittest.skipUnless(
    os.environ.get("RUN_RUNTIME_INTEGRATION") == "1", "Use tests/runtime-compose.yaml"
)
class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_empty_embedding_switch_preserves_data_and_refuses_a_used_corpus(self):
        from rag.runtime._qdrant import _switch_empty_embedding_profile

        original = self.state.settings.embedding_profile
        self.state.settings.embedding_profile = "new-empty-profile"
        self.state.settings.embedding_dimensions = 1024
        await _switch_empty_embedding_profile()
        await _switch_empty_embedding_profile()  # idempotent
        async with _transaction() as connection:
            row = await _one(connection, "SELECT embedding_profile, embedding_dimensions FROM corpus_state")
            self.assertNotEqual(row["embedding_profile"], original)
            self.assertEqual((row["embedding_profile"], row["embedding_dimensions"]), ("new-empty-profile", 1024))
            await _execute(connection, "UPDATE corpus_state SET generation=1")
        self.state.settings.embedding_profile = "another-profile"
        with self.assertRaises(StepError):
            await _switch_empty_embedding_profile()
        async with _transaction() as connection:
            await _execute(connection, "UPDATE corpus_state SET generation=0")
            await _execute(connection, "INSERT INTO documents(document_id,source_name) VALUES(:id,'retained-document')", id=uuid4())
        with self.assertRaises(StepError) as error:
            await _switch_empty_embedding_profile()
        self.assertEqual(error.exception.code, "embedding_profile_change_requires_reindex")
        async with _transaction() as connection:
            self.assertIsNotNone(await _one(connection, "SELECT document_id FROM documents WHERE source_name='retained-document'"))
            row = await _one(connection, "SELECT embedding_profile FROM corpus_state")
            self.assertEqual(row["embedding_profile"], "new-empty-profile")

    async def asyncSetUp(self):
        token = uuid4().hex
        self.schema = "runtime_" + token
        self.temporary = tempfile.TemporaryDirectory(prefix="runtime-test-")
        self.env = patch.dict(
            os.environ,
            {
                "RAG_PG_SCHEMA": self.schema,
                "RAG_CHUNKS_COLLECTION": "runtime_chunks_" + token,
                "RAG_TOPICS_COLLECTION": "runtime_topics_" + token,
                "RAG_CACHE_PREFIX": "runtime:" + token + ":",
                "RAG_QUEUE_PREFIX": "runtime." + token + ".",
                "RAG_S3_BUCKET": "runtime-" + token,
                "RAG_EMBEDDING_PROFILE": "runtime-test-v1",
                "RAG_EMBEDDING_DIMENSIONS": "3",
                "RAG_IO_TIMEOUT_S": os.environ.get("RAG_IO_TIMEOUT_S", "10"),
                "RAG_BATCH_SIZE": "1",
                "RAG_BROKER_RETRY_S": "0.01",
                "ADMIN_LOGIN": "runtime_admin",
                "ADMIN_PASSWORD": "isolated-admin-password",
                "WORKER_SPOOL_DIR": self.temporary.name,
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temporary.cleanup)
        await migrate_runtime()
        await start_runtime()
        self.state = _state._get()
        await rabbitmq._connect_broker()
        await prepare_runtime_storage()
        async with _transaction() as connection:
            await _execute(
                connection,
                "INSERT INTO employees(username,enabled) VALUES('employee_1',true),('employee_2',true),('employee_3',true)",
            )

    async def asyncTearDown(self):
        import xml.etree.ElementTree as ET

        try:
            for collection in (
                self.state.settings.chunks_collection,
                self.state.settings.topics_collection,
            ):
                if await self.state.qdrant.collection_exists(collection):
                    await self.state.qdrant.delete_collection(collection)
            status, _, body = await _s3("GET")
            if status == 200:
                for element in ET.fromstring(body).iter():
                    if element.tag.rsplit("}", 1)[-1] == "Key":
                        await _s3("DELETE", element.text)
                await _s3("DELETE")
            for route in ("rag.questions", "rag.results", "ingestion.jobs"):
                queue = await self.state.channel.declare_queue(
                    self.state.settings.queue_prefix + route, durable=True
                )
                await queue.delete(if_unused=False, if_empty=False)
            await self.state.engine.dispose()
            async with self.state.engine.begin() as connection:
                # Generated identifier, unique to this test. No deployed schema is touched.
                assert self.schema.startswith("runtime_") and len(self.schema) == 40
                await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        finally:
            await close_runtime()

    async def question(self, user=2, received=None, update=None):
        return await rag_api.register_question(
            NewQuestion(
                update_id=update or uuid4().int % 10**15,
                user_id=user,
                chat_id=user,
                username=f"employee_{user}",
                received_at=received or now(),
                input=TextQuestion(kind="text", text="Question"),
            )
        )

    async def finish(self, delivery_id):
        delivery = await rag_api.claim_delivery(delivery_id)
        self.assertIsNotNone(delivery)
        await rag_api.store_delivery_parts(
            delivery.attempt, [TextPart(kind="text", text="Delivered")]
        )
        await rag_api.record_delivery_part(delivery.attempt, 0, 77, "normal")
        self.assertTrue(await rag_api.complete_delivery(delivery.attempt, True, False))
        return delivery

    async def button(self, delivery_id, action, message_id):
        from rag.rag_api.contracts import InlineAction
        from rag.runtime.postgresql._callbacks import _callback_data

        delivery = await rag_api.claim_delivery(delivery_id)
        self.assertIsNotNone(delivery)
        await rag_api.store_delivery_parts(
            delivery.attempt,
            [
                TextPart(
                    kind="text",
                    text="Button",
                    actions=[InlineAction(text="Action", callback_data=_callback_data(action))],
                )
            ],
        )
        await rag_api.record_delivery_part(delivery.attempt, 0, message_id, "normal")
        await rag_api.complete_delivery(delivery.attempt, True, False)
        return delivery

    async def publish(self, document=None):
        revision = uuid4()
        accepted = await ingestion.register_revision(
            revision, document, f"uploads/{revision}",
            UploadSource(filename="Guide", access_level="employees"), []
        )
        job = await ingestion.claim_ingestion_job(revision)
        current = await ingestion.read_document_version("Guide")
        reserved = await ingestion.reserve_document_version(
            job.attempt, None if current is None else current.document_version)
        self.assertTrue(await ingestion.begin_revision_write(job.attempt, 1, 1))
        point = qdrant.ChunkPoint(
            chunk_id=uuid5(revision, "0"),
            document_id=accepted.document_id,
            revision_id=revision,
            source_name="Guide",
            text="Инструкция AUTH-17 установка",
            chunk_no=0,
            document_version=reserved.document_version,
            access_level="employees",
            dense=[1.0, 0.0, 0.0],
        )
        topic = qdrant.TopicPoint(
            point_id=uuid5(revision, "question:0"),
            revision_id=revision,
            question_no=0,
            text="Как установить AUTH-17?",
            dense=[1.0, 0.0, 0.0],
        )
        await qdrant.write_revision_points(revision, [point])
        await qdrant.write_topic_points(revision, [topic])
        generation = await ingestion.publish_revision(job.attempt)
        return accepted, point, topic, generation

    async def test_ingestion_from_docx_to_two_published_versions(self):
        from sample_docx import build_sample_docx
        from rag.ingestion import gateway, worker
        from rag.ingestion.contracts import JobRef, UploadInput
        from rag.model_gateway.contracts import EmbeddingResult, ImageDescription, ModelGenerationResult, OcrResult

        environment = {
            "INGESTION_EMBEDDING_PROFILE": "runtime-test-v1",
            "INGESTION_GENERATION_PROFILE": "fixture",
            "INGESTION_PREPARATION_PROFILE": "ingestion-v1",
            "INGESTION_EMBEDDING_DIMENSIONS": "3",
            "INGESTION_EMBEDDING_INPUT_CHARACTERS": "4000",
            "INGESTION_TOPIC_INPUT_CHARACTERS": "8000",
            "INGESTION_GENERATION_MAX_OUTPUT_TOKENS": "128",
        }
        async def embed(texts, purpose, **options):
            return EmbeddingResult(profile_id="runtime-test-v1", vectors=[[1.0, 0.0, 0.0] for _ in texts])
        async def stream():
            yield build_sample_docx()

        with patch.dict(os.environ, environment), \
                patch("rag.model_gateway.entrypoints.embed_texts", new=AsyncMock(side_effect=embed)), \
                patch("rag.model_gateway.entrypoints.generate", new=AsyncMock(return_value=ModelGenerationResult(
                    profile_id="fixture", text='{"questions":["How to save?"]}', finish_reason="completed", usage=None))), \
                patch("rag.model_gateway.entrypoints.describe_image", new=AsyncMock(return_value=ImageDescription(
                    profile_id="vision", description="Save button"))), \
                patch("rag.model_gateway.entrypoints.recognize_text", new=AsyncMock(return_value=OcrResult(
                    profile_id="ocr", text="Save"))):
            first = await gateway.accept_upload(UploadInput(content=stream(),
                source=UploadSource(filename="upload.docx", access_level="employees"), document_id=None))
            self.assertEqual(first.kind, "accepted")
            ref = f"uploads/{first.job_id}"
            batch = await upload_spool.load_document_batch(ref)
            self.assertIsNotNone(batch)
            for image in [image for chunk in batch.chunks for image in chunk.images]:
                self.assertEqual((await s3.get_image(image.image_id)).mime_type, "image/png")
            self.assertEqual(await worker.handle_ingestion_job(JobRef(job_id=first.job_id)), "ack")
            version = await ingestion.read_document_version("upload.docx")
            self.assertEqual((version.document_id, version.document_version), (first.document_id, 0))
            self.assertTrue(await qdrant.verify_revision_points(first.job_id, len(batch.chunks)))
            self.assertTrue(await qdrant.verify_topic_points(first.job_id, 1))
            self.assertEqual(await worker.handle_ingestion_job(JobRef(job_id=first.job_id)), "ack")
            second = await gateway.accept_upload(UploadInput(content=stream(),
                source=UploadSource(filename="upload.docx", access_level="employees"), document_id=None))
            self.assertEqual(second.document_id, first.document_id)
            self.assertEqual(await worker.handle_ingestion_job(JobRef(job_id=second.job_id)), "ack")
            self.assertEqual((await ingestion.read_document_version("upload.docx")).document_version, 1)
            async with _transaction() as connection:
                row = await _one(connection,
                    "SELECT current_revision_id FROM documents WHERE document_id=:id", id=first.document_id)
            self.assertEqual(row["current_revision_id"], second.job_id)

    async def test_atomic_admission_duplicates_and_revocation(self):
        results = await asyncio.gather(*(self.question() for _ in range(10)))
        self.assertEqual(sum(item.kind == "accepted" for item in results), 1)
        self.assertEqual(sum(item.kind == "busy" for item in results), 9)
        accepted = next(item for item in results if item.kind == "accepted")
        async with _transaction() as connection:
            row = await _one(
                connection,
                "SELECT update_id FROM rag_requests WHERE request_id=:id",
                id=accepted.request_id,
            )
        await support.write_user_access(
            Principal(login="runtime_admin"), AccessRecord(username="employee_2", enabled=False)
        )
        duplicate = await self.question(update=row["update_id"])
        self.assertEqual((duplicate.kind, duplicate.request_id), ("duplicate", accepted.request_id))
        self.assertEqual((await self.question()).kind, "denied")
        claimed = await rag_core.claim_question(accepted.request_id)
        self.assertIsNotNone(claimed)
        self.assertTrue(
            await rag_core.complete_question(
                claimed.attempt,
                NoticeOutcome(kind="notice", code="technical_error", text="Unavailable"),
            )
        )

    async def test_delivery_order_receipts_retry_and_slot_release(self):
        accepted = await self.question()
        claimed = await rag_core.claim_question(accepted.request_id)
        self.assertTrue(
            await rag_core.complete_question(
                claimed.attempt, AnsweredOutcome(kind="answered", result=answer())
            )
        )
        rows = await outbox.list_ready_outbox(20)
        delivery_id = next(
            row.notice.event.delivery_id for row in rows if row.notice.route == "rag.results"
        )
        delivery = await rag_api.claim_delivery(delivery_id)
        parts = [TextPart(kind="text", text="One"), TextPart(kind="text", text="Two")]
        self.assertTrue(await rag_api.store_delivery_parts(delivery.attempt, parts))
        with self.assertRaises(ValueError):
            await rag_api.complete_delivery(delivery.attempt, True, False)
        self.assertTrue(await rag_api.record_delivery_part(delivery.attempt, 0, 100, "normal"))
        async with _transaction() as connection:
            before = await _one(
                connection,
                "SELECT receipts FROM rag_deliveries WHERE delivery_id=:id",
                id=delivery_id,
            )
        await rag_api.record_delivery_part(delivery.attempt, 0, 100, "normal")
        self.assertTrue(
            await rag_api.schedule_delivery_retry(
                delivery.attempt, now() - timedelta(seconds=1), True
            )
        )
        retry = await rag_api.claim_delivery(delivery_id)
        self.assertEqual(retry.next_part, 1)
        self.assertFalse(await rag_api.record_delivery_part(delivery.attempt, 1, 101, "normal"))
        await rag_api.record_delivery_part(retry.attempt, 1, 101, "normal")
        await rag_api.complete_delivery(retry.attempt, True, False)
        self.assertEqual((await self.question()).kind, "accepted")
        async with _transaction() as connection:
            after = await _one(
                connection,
                "SELECT receipts FROM rag_deliveries WHERE delivery_id=:id",
                id=delivery_id,
            )

        def data(value):
            return json.loads(value) if isinstance(value, str) else value

        self.assertEqual(data(before["receipts"])[0], data(after["receipts"])[0])

    async def test_published_generation_pins_two_indexes_until_completion(self):
        old, point, topic, generation = await self.publish()
        accepted = await self.question()
        question = await rag_core.claim_question(accepted.request_id)
        self.assertEqual(question.corpus_generation, generation)
        new, _, _, new_generation = await self.publish(old.document_id)
        self.assertEqual(new_generation, generation + 1)
        self.assertEqual(await rag_core.load_revision_ids(generation), frozenset({old.job_id}))
        self.assertEqual(await cleanup.claim_revision_cleanup(20), [])
        hits = await qdrant.find_chunks(
            "AUTH-17", [1.0, 0.0, 0.0], frozenset({old.job_id}), 5, lexical_priority=True
        )
        self.assertEqual([hit.fragment.revision_id for hit in hits], [old.job_id])
        self.assertEqual(
            (await qdrant.find_topic_matches([1.0, 0.0, 0.0], frozenset({new.job_id})))[
                0
            ].revision_id,
            new.job_id,
        )
        await rag_core.complete_question(
            question.attempt, NoticeOutcome(kind="notice", code="blocked", text="Blocked")
        )
        self.assertEqual(await cleanup.claim_revision_cleanup(20), [old.job_id])
        await qdrant.delete_revision_points(old.job_id)
        self.assertTrue(await qdrant.verify_revision_points(old.job_id, 0))
        self.assertTrue(await qdrant.verify_topic_points(old.job_id, 0))
        self.assertTrue(await cleanup.finish_revision_cleanup(old.job_id))
        self.assertEqual((await load_source_metadata(old.document_id)).source_name, "Guide")

    async def test_pending_write_blocks_retry_and_cleanup(self):
        revision = uuid4()
        await ingestion.register_revision(
            revision, None, f"uploads/{revision}",
            UploadSource(filename="Guide", access_level="employees"), [],
        )
        job = await ingestion.claim_ingestion_job(revision)
        await ingestion.reserve_document_version(job.attempt, None)
        await ingestion.begin_revision_write(job.attempt, 1, 1)
        self.assertFalse(await ingestion.schedule_ingestion_retry(job.attempt, now()))
        self.assertFalse(await ingestion.fail_ingestion_job(job.attempt))
        async with _transaction() as connection:
            await _execute(
                connection,
                "UPDATE document_revisions SET resume_at=:past WHERE revision_id=:id",
                past=now() - timedelta(seconds=1),
                id=revision,
            )
        self.assertIsNone(await ingestion.claim_ingestion_job(revision))
        self.assertEqual(await outbox.recover_pending_work("ingestion.jobs", 20), 0)
        pending = await ingestion.list_pending_revision_writes(20)
        self.assertEqual(pending[0].revision_id, revision)
        self.assertFalse(await ingestion.clear_revision_write_pending(revision, 999))
        self.assertTrue(
            await ingestion.clear_revision_write_pending(revision, job.attempt.attempt_no)
        )
        retry = await ingestion.claim_ingestion_job(revision)
        with self.assertRaisesRegex(StepError, "topic_count_mismatch"):
            await ingestion.begin_revision_write(retry.attempt, 1, 2)
        self.assertTrue(await ingestion.fail_ingestion_job(retry.attempt))

    async def test_returned_pending_attempt_immediately_allows_other_documents(self):
        first, second, next_revision = uuid4(), uuid4(), uuid4()
        source = UploadSource(filename="Pending A", access_level="employees")
        accepted = await ingestion.register_revision(first, None, f"uploads/{first}", source, [])
        await ingestion.register_revision(
            second, None, f"uploads/{second}",
            UploadSource(filename="Independent B", access_level="employees"), [],
        )
        await ingestion.register_revision(
            next_revision, accepted.document_id, f"uploads/{next_revision}", source, [],
        )
        job = await ingestion.claim_ingestion_job(first)
        await ingestion.reserve_document_version(job.attempt, None)
        self.assertTrue(await ingestion.begin_revision_write(job.attempt, 2, 3))

        # pending=true also exists during a live write: it must still exclude B.
        self.assertGreater(job.attempt.deadline_at, now())
        self.assertIsNone(await ingestion.claim_ingestion_job(second))
        self.assertTrue(await ingestion._release_pending_attempt(job.attempt))
        async with _transaction() as connection:
            row = await _one(connection,
                "SELECT state, resume_at, qdrant_write_pending, attempt_no, "
                "staging_ref, expected_chunk_count, expected_topic_count "
                "FROM document_revisions WHERE revision_id=:id", id=first)
        self.assertEqual(row["state"], "indexing")
        self.assertTrue(row["qdrant_write_pending"])
        self.assertLessEqual(row["resume_at"], now())
        self.assertEqual(row["attempt_no"], job.attempt.attempt_no)
        self.assertEqual(row["staging_ref"], f"uploads/{first}")
        self.assertEqual((row["expected_chunk_count"], row["expected_topic_count"]), (2, 3))

        # No clock manipulation or reconciliation is needed to start document B.
        other = await ingestion.claim_ingestion_job(second)
        self.assertIsNotNone(other)
        self.assertLess(now(), job.attempt.deadline_at)
        self.assertIsNone(await ingestion.claim_ingestion_job(first))
        self.assertFalse(await ingestion.schedule_ingestion_retry(job.attempt, now()))
        self.assertFalse(await ingestion.fail_ingestion_job(job.attempt))
        self.assertIsNone(await ingestion.publish_revision(job.attempt))
        self.assertFalse(await ingestion._release_pending_attempt(job.attempt))
        self.assertEqual(await cleanup.list_finished_staging(20), [])
        self.assertEqual(await cleanup.claim_revision_cleanup(20), [])

        # Finishing B doesn't unblock a later revision of A or recreate A's notice.
        self.assertTrue(await ingestion.fail_ingestion_job(other.attempt))
        self.assertIsNone(await ingestion.claim_ingestion_job(next_revision))
        self.assertEqual(await outbox.recover_pending_work("ingestion.jobs", 20), 0)
        pending = await ingestion.list_pending_revision_writes(20)
        self.assertEqual([(item.revision_id, item.attempt_no) for item in pending],
                         [(first, job.attempt.attempt_no)])

    async def test_redis_compare_token_and_exact_large_generations(self):
        result = answer()
        value = redis.CachedAnswer(**result.model_dump(), corpus_generation=2**53 + 2)
        self.assertTrue(await redis.write_cached_answer_if_not_older("Question", value))
        old = await redis.read_cached_answer("Question")
        self.assertTrue(await redis.write_cached_answer_if_not_older("Question", value))
        self.assertFalse(
            await redis.delete_cached_answer_if_unchanged("Question", old.compare_token)
        )
        older = redis.CachedAnswer(**result.model_dump(), corpus_generation=2**53 + 1)
        self.assertFalse(await redis.write_cached_answer_if_not_older("Question", older))
        current = await redis.read_cached_answer("Question")
        self.assertEqual(current.value.corpus_generation, 2**53 + 2)
        self.assertTrue(
            await redis.delete_cached_answer_if_unchanged("Question", current.compare_token)
        )
        self.assertIsNone(await redis.read_cached_answer("Question"))

    async def test_s3_idempotence_conflict_and_private_bucket(self):
        image_id = uuid4()
        png = base64.b64decode(
            "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
        )
        image = ImageContent(data=png, mime_type="image/png")
        await asyncio.gather(s3.put_image(image_id, image), s3.put_image(image_id, image))
        self.assertEqual(await s3.get_image(image_id), image)
        with self.assertRaisesRegex(StepError, "image_conflict"):
            await s3.put_image(
                image_id, ImageContent(data=png + b"different", mime_type="image/png")
            )
        with self.assertRaisesRegex(StepError, "image_not_found"):
            await s3.get_image(uuid4())
        response = await self.state.http.get(
            self.state.settings.s3_endpoint_url
            + "/"
            + self.state.settings.s3_bucket
            + "/images/"
            + str(image_id)
        )
        self.assertEqual(response.status_code, 403)

    async def test_spool_snapshot_is_durable_immutable_and_checked(self):
        revision = uuid4()
        ref = await upload_spool.stage_upload(
            revision, ConvertedDocument(markdown="Document"),
            UploadSource(filename="Guide", access_level="employees")
        )
        self.assertEqual(
            ref,
            await upload_spool.stage_upload(
                revision, ConvertedDocument(markdown="Document"),
                UploadSource(filename="Guide", access_level="employees")
            ),
        )
        staged = await upload_spool.open_staged_upload(ref)
        point = qdrant.ChunkPoint(
            chunk_id=uuid5(revision, "0"),
            document_id=uuid4(),
            revision_id=revision,
            source_name="Guide",
            text="Document",
            chunk_no=0,
            document_version=0,
            access_level="employees",
            dense=[1.0, 0.0, 0.0],
        )
        topic = qdrant.TopicPoint(
            point_id=uuid5(revision, "question:0"),
            revision_id=revision,
            question_no=0,
            text="Question",
            dense=[1.0, 0.0, 0.0],
        )
        prepared = upload_spool.PreparedRevision(
            revision_id=revision,
            preparation_profile="ingestion-v1",
            embedding_profile="runtime-test-v1",
            points=[point],
            topic_points=[topic],
        )
        await upload_spool.store_prepared_revision(ref, prepared)
        self.assertEqual(await upload_spool.load_prepared_revision(ref), prepared)
        changed = prepared.model_copy(
            update={"points": [point.model_copy(update={"text": "Changed"})]}
        )
        with self.assertRaisesRegex(StepError, "prepared_revision_conflict"):
            await upload_spool.store_prepared_revision(ref, changed)
        (staged.path.parent / "prepared.json").write_text("invalid", encoding="utf-8")
        with self.assertRaises(StorageError):
            await upload_spool.load_prepared_revision(ref)
        self.assertIn(
            ref, await upload_spool.list_staging_candidates(now() + timedelta(seconds=1), 10)
        )
        with self.assertRaises(ValueError):
            await upload_spool.remove_staged_upload("uploads/../escape")
        await upload_spool.remove_staged_upload(ref)
        self.assertIsNone(await upload_spool.open_staged_upload(ref))

    async def test_automatic_support_authorization_and_message_idempotence(self):
        accepted = await self.question()
        question = await rag_core.claim_question(accepted.request_id)
        await rag_core.complete_question(
            question.attempt, SupportOutcome(kind="support", reason="no_evidence")
        )
        case = (await support.list_pending_support(now(), 10))[0]
        self.assertEqual((await self.question()).kind, "busy")
        ticket = await support.create_support_ticket(
            case.case_id, "https://example.test/tickets/" + str(case.case_id)
        )
        self.assertEqual(
            await support.create_support_ticket(case.case_id, "https://other.test/"), ticket
        )
        owner = None
        message_id = uuid4()
        first = await support.append_ticket_message(owner, case.case_id, message_id, "Help")
        self.assertEqual(
            await support.append_ticket_message(owner, case.case_id, message_id, "Help"), first
        )
        with self.assertRaises(Conflict):
            await support.append_ticket_message(owner, case.case_id, message_id, "Changed")
        with self.assertRaises(AccessDenied):
            await support.close_ticket_record(Principal(login="wrong_admin"), case.case_id)
        view = await support.read_ticket(case.case_id)
        self.assertEqual(view.history[0].question_text, "Question")
        admin = Principal(login="runtime_admin")
        closed = await support.close_ticket_record(admin, case.case_id)
        self.assertEqual(await support.close_ticket_record(admin, case.case_id), closed)
        metrics = await support.read_metrics(
            admin, now() - timedelta(days=1), now() + timedelta(seconds=1)
        )
        self.assertEqual((metrics.requests, metrics.escalated), (1, 1))

    async def test_relay_persistent_body_and_recovery_window(self):
        accepted = await self.question()
        self.assertEqual(await relay_outbox_batch(20), 1)
        queue = await self.state.channel.declare_queue(
            self.state.settings.queue_prefix + "rag.questions", durable=True
        )
        message = await queue.get(timeout=5)
        self.assertEqual(json.loads(message.body), {"request_id": accepted.request_id})
        self.assertEqual(message.delivery_mode, aio_pika.DeliveryMode.PERSISTENT)
        await message.ack()
        self.assertEqual(await outbox.recover_pending_work("rag.questions", 20), 0)
        async with _transaction() as connection:
            await _execute(
                connection,
                "UPDATE outbox SET published_at=:past",
                past=now() - timedelta(minutes=2),
            )
        self.assertEqual(await outbox.recover_pending_work("rag.questions", 20), 1)
        self.assertEqual(await outbox.recover_pending_work("rag.questions", 20), 0)

    async def test_manual_support_timer_confirmation_and_ownership(self):
        accepted = await self.question()
        claimed = await rag_core.claim_question(accepted.request_id)
        await rag_core.complete_question(
            claimed.attempt, AnsweredOutcome(kind="answered", result=answer())
        )
        rows = await outbox.list_ready_outbox(20)
        delivery_id = next(
            row.notice.event.delivery_id for row in rows if row.notice.route == "rag.results"
        )
        await self.finish(delivery_id)

        async def command(kind, user=2, message=None, **fields):
            return await support.record_support_action(
                SupportCommand(
                    update_id=uuid4().int % 10**15,
                    user_id=user,
                    chat_id=user,
                    username=f"employee_{user}",
                    received_at=now(),
                    origin="text" if message is None else "callback",
                    callback_message_id=message,
                    action=SupportAction(kind=kind, **fields),
                ),
                deadline if kind == "start" else None,
            )

        deadline = now() + timedelta(minutes=10)
        offered = await command(
            "offer", reason="dissatisfied"
        )  # Latest confirmed answer, no request_id.
        self.assertEqual(offered.request_id, accepted.request_id)
        await self.button(
            offered.delivery_id, SupportAction(kind="start", request_id=accepted.request_id), 78
        )
        started = await command("start", message=78, request_id=accepted.request_id)
        deadline += timedelta(minutes=10)
        repeated = await command("start", message=78, request_id=accepted.request_id)
        self.assertEqual(repeated.case_id, started.case_id)
        self.assertEqual(await support.list_pending_support(now(), 20), [])
        self.assertFalse(await support.queue_support_confirmation(started.case_id, now()))
        cases = await support.list_pending_support(deadline, 20)
        self.assertEqual(cases[0].check_at, deadline - timedelta(minutes=10))
        self.assertEqual(
            (await command("confirm", message=79, case_id=started.case_id)).kind, "ignored"
        )
        self.assertTrue(await support.queue_support_confirmation(started.case_id, deadline))
        async with _transaction() as connection:
            waiting = await _one(
                connection, "SELECT delivery_id FROM rag_deliveries WHERE purpose='notice'"
            )
            confirmation = await _one(
                connection,
                "SELECT delivery_id FROM rag_deliveries WHERE purpose='support_confirmation'",
            )
        await self.finish(waiting["delivery_id"])
        await self.button(
            confirmation["delivery_id"], SupportAction(kind="confirm", case_id=started.case_id), 79
        )
        self.assertEqual(
            (await command("confirm", user=3, message=79, case_id=started.case_id)).kind, "ignored"
        )
        self.assertEqual(
            (await command("confirm", message=79, case_id=started.case_id)).kind, "handled"
        )
        ticket = await support.create_support_ticket(started.case_id, "https://example.test/ticket")
        self.assertEqual(ticket.reason, "dissatisfied")
        self.assertEqual(
            (await self.question()).kind, "accepted"
        )  # Manual support holds no active slot.

    async def test_broker_consumer_invalid_event_retry_and_ack(self):
        from rag.contracts import RequestRef

        stop, finished = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler(event):
            calls.append(event.request_id)
            if len(calls) == 1:
                raise RuntimeError("Injected storage failure")
            finished.set()
            return "ack"

        consumer = asyncio.create_task(rabbitmq._consume("rag.questions", handler, stop))
        try:
            await self.state.channel.default_exchange.publish(
                aio_pika.Message(
                    body=b"invalid", message_id=str(uuid4()), content_type="application/json"
                ),
                routing_key=self.state.settings.queue_prefix + "rag.questions",
                mandatory=True,
            )
            await rabbitmq.publish_notice(
                rabbitmq.QuestionNotice(
                    message_id=uuid4(), route="rag.questions", event=RequestRef(request_id=123)
                )
            )
            await asyncio.wait_for(finished.wait(), timeout=10)
        finally:
            stop.set()
            await asyncio.wait_for(consumer, timeout=10)
        self.assertEqual(calls, [123, 123])
        queue = await self.state.channel.declare_queue(
            self.state.settings.queue_prefix + "rag.questions", durable=True
        )
        self.assertIsNone(await queue.get(fail=False, timeout=2))

    async def test_failed_confirm_keeps_outbox_and_unroutable_publish_fails(self):
        await self.question()
        with patch(
            "rag.runtime.queue_relay.publish_notice",
            new=AsyncMock(side_effect=rabbitmq.BrokerPublishError("Injected")),
        ):
            with self.assertRaises(rabbitmq.BrokerPublishError):
                await relay_outbox_batch(20)
        self.assertEqual(len(await outbox.list_ready_outbox(20)), 1)
        row = (await outbox.list_ready_outbox(20))[0]
        queue = await self.state.channel.declare_queue(
            self.state.settings.queue_prefix + "rag.questions", durable=True
        )
        await queue.delete(if_unused=False, if_empty=False)
        with self.assertRaises(rabbitmq.BrokerPublishError):
            await rabbitmq.publish_notice(row.notice)
        self.assertEqual(len(await outbox.list_ready_outbox(20)), 1)

    async def test_admission_cap_stale_status_and_expired_attempt(self):
        self.state.settings.max_pending_requests = 1
        accepted = await self.question(received=now() - timedelta(seconds=11))
        self.assertEqual((await self.question(user=3)).kind, "retry_later")
        self.assertEqual(await rag_api.queue_waiting_statuses(now(), 20), 1)
        self.assertEqual(await rag_api.queue_waiting_statuses(now(), 20), 0)
        claimed = await rag_core.claim_question(accepted.request_id)
        async with _transaction() as connection:
            await _execute(
                connection,
                "UPDATE rag_requests SET resume_at=:past WHERE request_id=:id",
                id=accepted.request_id,
                past=now() - timedelta(seconds=1),
            )
        self.assertFalse(
            await rag_core.complete_question(
                claimed.attempt, AnsweredOutcome(kind="answered", result=answer())
            )
        )
        retry = await rag_core.claim_question(accepted.request_id)
        self.assertEqual(retry.corpus_generation, claimed.corpus_generation)
        self.assertEqual(retry.attempt.attempt_no, claimed.attempt.attempt_no + 1)
        await rag_core.complete_question(
            retry.attempt, AnsweredOutcome(kind="answered", result=answer())
        )
        async with _transaction() as connection:
            status = await _one(
                connection, "SELECT delivery_id FROM rag_deliveries WHERE purpose='status'"
            )
        self.assertIsNone(await rag_api.claim_delivery(status["delivery_id"]))
        self.assertEqual(
            (await self.question(user=3)).kind, "retry_later"
        )  # Terminal delivery still holds slot.

    async def test_missing_index_is_recovery_error_without_recreating_it(self):
        await self.publish()
        await self.state.qdrant.delete_collection(self.state.settings.topics_collection)
        with self.assertRaises(StepError):
            await prepare_runtime_storage()
        self.assertFalse(
            await self.state.qdrant.collection_exists(self.state.settings.topics_collection)
        )

    async def test_start_is_idempotent_and_admission_does_not_connect_broker(self):
        await close_runtime()
        with patch(
            "rag.runtime.rabbitmq.aio_pika.connect_robust",
            new=AsyncMock(side_effect=OSError("Injected offline broker")),
        ) as connect:
            await asyncio.gather(start_runtime(), start_runtime(), start_runtime())
            self.state = _state._get()
            self.assertIsNone(self.state.broker)
            self.assertEqual((await self.question()).kind, "accepted")
            connect.assert_not_awaited()
            with self.assertRaises(rabbitmq.BrokerPublishError):
                await relay_outbox_batch(20)
            self.assertEqual(len(await outbox.list_ready_outbox(20)), 1)
        await rabbitmq._connect_broker()  # Restore test resources for its isolated teardown.

    async def test_operational_commands_relay_shutdown_and_singleton(self):
        from rag.runtime.runner import _singleton

        async def command(*arguments):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "rag.runtime",
                *arguments,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                output, error = await asyncio.wait_for(process.communicate(), timeout=15)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            return process.returncode, output, error

        for operation in ("migrate", "migrate", "prepare", "pending-writes"):
            code, _, error = await command(operation)
            self.assertEqual(code, 0, error.decode())
        self.assertNotEqual((await command("bootstrap-admin", "1"))[0], 0)
        self.assertEqual((await command("set-access", "@Employee_4"))[0], 0)
        self.assertTrue((await support.read_user_access("employee_4")).enabled)
        self.assertEqual((await command("set-access", "employee_4", "--disable"))[0], 0)
        self.assertFalse((await support.read_user_access("employee_4")).enabled)
        async with _singleton("relay"):
            with self.assertRaises(RuntimeError):
                async with _singleton("relay"):
                    self.fail("A second relay must not become active")
        await self.question()
        relay = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "rag.runtime",
            "serve",
            "relay",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            queue = await self.state.channel.declare_queue(
                self.state.settings.queue_prefix + "rag.questions", durable=True
            )
            async with asyncio.timeout(15):
                message = None
                while message is None:
                    message = await queue.get(fail=False, timeout=2)
                    if message is None:
                        await asyncio.sleep(0.05)
            await message.ack()
        finally:
            relay.terminate()
            try:
                _, error = await asyncio.wait_for(relay.communicate(), timeout=15)
            except BaseException:
                relay.kill()
                await relay.wait()
                raise
        self.assertEqual(relay.returncode, 0, error.decode())
        self.assertEqual(await outbox.list_ready_outbox(20), [])

    async def test_callback_receipt_race_and_forged_actions(self):
        from rag.rag_api.entrypoints import build_delivery_parts
        from rag.rag_api.intake import parse_action

        accepted = await self.question()
        question = await rag_core.claim_question(accepted.request_id)
        await rag_core.complete_question(
            question.attempt, AnsweredOutcome(kind="answered", result=answer())
        )
        rows = await outbox.list_ready_outbox(20)
        delivery_id = next(
            row.notice.event.delivery_id for row in rows if row.notice.route == "rag.results"
        )
        delivery = await rag_api.claim_delivery(delivery_id)
        parts = build_delivery_parts(delivery, "https://bot.example")
        action = parse_action(parts[0].actions[0].callback_data)
        self.assertEqual(action, SupportAction(kind="offer", request_id=accepted.request_id,
                                              reason="dissatisfied"))
        await rag_api.store_delivery_parts(delivery.attempt, parts)
        command = SupportCommand(
            update_id=uuid4().int % 10**15,
            user_id=2,
            chat_id=2,
            username="employee_2",
            received_at=now(),
            origin="callback",
            callback_message_id=9000,
            action=action,
        )
        self.assertEqual((await support.record_support_action(command, None)).kind, "retry_later")
        async with _transaction() as connection:
            self.assertIsNone(
                await _one(
                    connection,
                    "SELECT update_id FROM telegram_updates WHERE update_id=:id",
                    id=command.update_id,
                )
            )
        await rag_api.record_delivery_part(delivery.attempt, 0, 9000, "normal")
        await rag_api.complete_delivery(delivery.attempt, True, False)
        applied = await support.record_support_action(command, None)
        self.assertEqual(applied.kind, "handled")
        for fields in (
            {"user_id": 3, "username": "employee_3"},
            {"chat_id": 777},
            {"callback_message_id": 9001},
            {
                "action": SupportAction(
                    kind="offer", request_id=accepted.request_id, reason="human_request"
                )
            },
            {"action": action.model_copy(update={"case_id": uuid4()})},
        ):
            with self.subTest(fields=fields):
                forged = command.model_copy(update={"update_id": uuid4().int % 10**15, **fields})
                self.assertEqual(
                    (await support.record_support_action(forged, None)).kind, "ignored"
                )
        await support.write_user_access(
            Principal(login="runtime_admin"), AccessRecord(username="employee_2", enabled=False)
        )
        self.assertEqual(await support.record_support_action(command, None), applied)
        self.assertEqual(
            (
                await support.record_support_action(
                    command.model_copy(update={"update_id": uuid4().int % 10**15}), None
                )
            ).kind,
            "denied",
        )

    async def test_username_lock_serializes_revocation_and_missing_username_denies(self):
        from rag.runtime.postgresql._db import _lock

        missing = NewQuestion(
            update_id=uuid4().int % 10**15,
            user_id=2,
            chat_id=2,
            received_at=now(),
            input=TextQuestion(kind="text", text="Question"),
        )
        self.assertEqual((await rag_api.register_question(missing)).kind, "denied")
        self.assertIsNone(await support.read_user_access(None))
        async with _transaction() as connection:
            await _lock(connection, "username", "employee_2")
            await _execute(
                connection, "UPDATE employees SET enabled=false WHERE username='employee_2'"
            )
            task = asyncio.create_task(self.question())
            await asyncio.sleep(0.05)
            self.assertFalse(task.done())
        self.assertEqual((await task).kind, "denied")
        admin = Principal(login="runtime_admin")
        await support.write_user_access(admin, AccessRecord(username="employee_2", enabled=True))
        accepted = await self.question()
        async with _transaction() as connection:
            row = await _one(
                connection,
                "SELECT username FROM rag_requests WHERE request_id=:id",
                id=accepted.request_id,
            )
        self.assertEqual(row["username"], "employee_2")
        # Changing the current name does not change the active slot's User ID.
        renamed = missing.model_copy(
            update={"update_id": uuid4().int % 10**15, "username": "employee_3"}
        )
        self.assertEqual((await rag_api.register_question(renamed)).kind, "busy")

    async def test_public_messages_admin_isolation_and_replay_after_close(self):
        from rag.rag_api.contracts import NotFound

        accepted = await self.question()
        question = await rag_core.claim_question(accepted.request_id)
        await rag_core.complete_question(
            question.attempt, SupportOutcome(kind="support", reason="no_evidence")
        )
        case = (await support.list_pending_support(now(), 10))[0]
        with self.assertRaises(NotFound):
            await support.read_ticket(case.case_id)
        await support.create_support_ticket(case.case_id, "https://example.test/ticket")
        admin = Principal(login="runtime_admin")
        key = uuid4()
        visitor = await support.append_ticket_message(None, case.case_id, key, "Visitor text")
        operator = await support.append_ticket_message(admin, case.case_id, key, "Operator text")
        self.assertEqual(visitor.author_role, "visitor")
        self.assertIsNone(visitor.author_login)
        self.assertEqual((operator.author_role, operator.author_login), ("operator", admin.login))
        self.assertNotEqual(visitor.message_id, operator.message_id)
        closed = await support.close_ticket_record(admin, case.case_id)
        self.assertEqual(await support.close_ticket_record(admin, case.case_id), closed)
        self.assertEqual(
            await support.append_ticket_message(None, case.case_id, key, "Visitor text"), visitor
        )
        with self.assertRaises(Conflict):
            await support.append_ticket_message(None, case.case_id, key, "Changed")
        with self.assertRaises(Conflict):
            await support.append_ticket_message(None, case.case_id, uuid4(), "New text")
        with self.assertRaises(AccessDenied):
            await support.read_ticket_list(Principal(login="untrusted"), 20)
        from pydantic import SecretStr

        self.state.settings.admin_password = SecretStr("")
        with self.assertRaises(AccessDenied):
            await support.read_ticket_list(admin, 20)
        view = await support.read_ticket(
            case.case_id
        )  # Public access survives missing admin configuration.
        self.assertEqual(len(view.messages), 2)

    async def test_orm_models_match_migrated_database_columns(self):
        from sqlalchemy import inspect
        from rag.runtime.postgresql._models import _Base

        def check(connection):
            inspector = inspect(connection)
            for table in _Base.metadata.sorted_tables:
                actual = {column["name"]: column for column in inspector.get_columns(table.name)}
                self.assertEqual(set(actual), set(table.columns.keys()), table.name)
                for column in table.columns:
                    with self.subTest(table=table.name, column=column.name):
                        self.assertEqual(column.nullable, actual[column.name]["nullable"])
                        self.assertEqual(
                            column.type.compile(dialect=connection.dialect),
                            actual[column.name]["type"].compile(dialect=connection.dialect),
                        )

        async with self.state.engine.connect() as connection:
            await connection.run_sync(check)

    async def test_readiness_writes_roll_back_and_reject_read_only_or_full_database(self):
        from contextlib import asynccontextmanager

        async def counts():
            async with _transaction() as session:
                return await _one(session, 'SELECT (SELECT count(*) FROM rag_requests) AS requests, '
                                  '(SELECT count(*) FROM outbox) AS notices, '
                                  '(SELECT count(*) FROM telegram_updates) AS updates')
        initial = dict(await counts())
        self.assertTrue(await rag_api.check_admission_ready())
        self.assertEqual(dict(await counts()), initial)
        @asynccontextmanager
        async def read_only():
            async with _transaction() as session:
                await _execute(session, 'SET TRANSACTION READ ONLY')
                yield session
        with patch.object(rag_api, '_transaction', read_only):
            with self.assertRaises(StorageError):
                await rag_api.check_admission_ready()
        self.assertEqual(dict(await counts()), initial)
        self.state.settings.max_pending_requests = 1
        await self.question()
        self.assertFalse(await rag_api.check_admission_ready())

    async def test_public_rate_limit_is_atomic_and_pagination_has_no_gaps(self):
        from rag.rag_api.contracts import RateLimited
        from pydantic import ValidationError

        accepted = await self.question()
        question = await rag_core.claim_question(accepted.request_id)
        await rag_core.complete_question(question.attempt, SupportOutcome(kind='support', reason='no_evidence'))
        case = (await support.list_pending_support(now(), 10))[0]
        await support.create_support_ticket(case.case_id, f'https://bot.example/tickets/{case.case_id}')
        keys = [uuid4() for _ in range(23)]
        replies = await asyncio.gather(*(support.append_ticket_message(None, case.case_id, key, 'Visitor')
                                         for key in keys), return_exceptions=True)
        accepted_replies = [reply for reply in replies if not isinstance(reply, Exception)]
        self.assertEqual(len(accepted_replies), 20)
        self.assertEqual(sum(isinstance(reply, RateLimited) for reply in replies), 3)
        first_index = next(i for i, reply in enumerate(replies) if not isinstance(reply, Exception))
        self.assertEqual(await support.append_ticket_message(None, case.case_id, keys[first_index], 'Visitor'),
                         replies[first_index])
        with self.assertRaises(ValidationError):
            await support.append_ticket_message(None, case.case_id, uuid4(), 'x' * 4097)
        # Operators remain able to respond while a visitor is at the limit.
        operator = await support.append_ticket_message(Principal(login='runtime_admin'), case.case_id, uuid4(), 'Operator')
        seen, cursor = [], None
        while True:
            view = await support.read_ticket(case.case_id, 7, cursor)
            seen.extend(message.message_id for message in view.messages)
            if view.next_cursor is None:
                break
            cursor = view.next_cursor
        expected = {reply.message_id for reply in accepted_replies} | {operator.message_id}
        self.assertEqual(set(seen), expected)
        self.assertEqual(len(seen), len(expected))
        async with _transaction() as session:
            await _execute(session, "UPDATE support_messages SET created_at=clock_timestamp()-interval '61 seconds' WHERE case_id=:id", id=case.case_id)
        await support.append_ticket_message(None, case.case_id, uuid4(), 'After window')

    async def test_actual_fallback_is_saved_in_ticket_and_metrics_use_one_query(self):
        from sqlalchemy import event
        from rag.rag_api.entrypoints import handle_rag_result
        from rag.rag_api.delivery import sender

        expected_text, fallback_request = None, None
        with patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN':'123:test', 'TELEGRAM_WEBHOOK_SECRET':'test-secret',
                                     'TELEGRAM_WEBHOOK_URL':'https://bot.example'}):
            for user, text_value in ((2, 'Long answer ' * 1000), (3, 'Normal answer')):
                accepted = await self.question(user=user)
                question = await rag_core.claim_question(accepted.request_id)
                result = answer().model_copy(update={'answer_text': text_value})
                await rag_core.complete_question(question.attempt, AnsweredOutcome(kind='answered', result=result))
                async with _transaction() as session:
                    row = await _one(session, "SELECT delivery_id FROM rag_deliveries WHERE request_id=:id AND purpose='answer'", id=accepted.request_id)
                with patch.object(sender.adapter, '_post', AsyncMock(return_value=(200, {'ok':True,'result':{'message_id':321}}))):
                    self.assertEqual(await handle_rag_result(DeliveryRef(delivery_id=row['delivery_id'])), 'ack')
                if user == 2:
                    fallback_request = accepted.request_id
                    async with _transaction() as session:
                        saved = await _one(session, 'SELECT parts,receipts FROM rag_deliveries WHERE delivery_id=:id', id=row['delivery_id'])
                    self.assertEqual(saved['receipts'][0]['variant'], 'length_fallback')
                    expected_text = saved['receipts'][0]['delivered_text']
                    self.assertEqual(expected_text, saved['parts'][0]['text'])
                    self.assertNotIn('Long answer', expected_text)
        # A delivered fallback still supports the genuine dissatisfaction action.
        applied = await support.record_support_action(SupportCommand(update_id=uuid4().int % 10**15,
            user_id=2, chat_id=2, username='employee_2', received_at=now(), origin='callback', callback_message_id=321,
            action=SupportAction(kind='offer', request_id=fallback_request, reason='dissatisfied')), None)
        self.assertEqual(applied.kind, 'handled')
        case_id = uuid4()
        async with _transaction() as session:
            await _execute(session, "INSERT INTO support_cases(case_id,request_id,user_id,state,reason) VALUES(:case,:request,2,'creating','dissatisfied')",
                           case=case_id, request=fallback_request)
        await support.create_support_ticket(case_id, f'https://bot.example/tickets/{case_id}')
        view = await support.read_ticket(case_id)
        self.assertEqual(view.history[0].answer_text, expected_text)
        await support.close_ticket_record(Principal(login='runtime_admin'), case_id)
        queries = []
        def counted(conn, cursor, statement, parameters, context, executemany):
            queries.append(statement)
        event.listen(self.state.engine.sync_engine, 'before_cursor_execute', counted)
        try:
            metrics = await support.read_metrics(Principal(login='runtime_admin'), now()-timedelta(days=1), now()+timedelta(seconds=1))
        finally:
            event.remove(self.state.engine.sync_engine, 'before_cursor_execute', counted)
        self.assertEqual(len(queries), 1)
        self.assertEqual((metrics.requests, metrics.answered, metrics.escalated, metrics.dissatisfied), (2,2,1,1))
        self.assertEqual(len(metrics.reply_duration_ms), 1)
        self.assertEqual(len(metrics.ticket_duration_ms), 1)
        empty = await support.read_metrics(Principal(login='runtime_admin'), now()+timedelta(days=1), now()+timedelta(days=2))
        self.assertEqual((empty.requests, empty.answered, empty.escalated, empty.dissatisfied), (0,0,0,0))

    async def test_orphan_sweep_waits_for_unknown_registration_commit(self):
        import os
        from rag.runtime._uploads import _protect_upload, _sweep_orphans

        revision = uuid4()
        source = UploadSource(filename='Orphan test', access_level='employees')
        ref = await upload_spool.stage_upload(revision, ConvertedDocument(markdown='Document'), source)
        path = self.state.settings.spool_dir / ref
        os.utime(path, (1,1))
        async with _protect_upload(revision):
            self.assertEqual(await _sweep_orphans(1), 0)
            self.assertTrue(path.exists())
            # Registration commits, but the caller loses its response.
            accepted = await ingestion.register_revision(revision, None, ref, source, [])
        self.assertEqual(await _sweep_orphans(1), 0)
        self.assertTrue(path.exists())
        other = uuid4()
        other_ref = await upload_spool.stage_upload(other, ConvertedDocument(markdown='Orphan'), source)
        os.utime(self.state.settings.spool_dir / other_ref, (1,1))
        # A registered old directory must not starve the next page of candidates.
        self.assertEqual(await _sweep_orphans(1), 1)
        self.assertFalse((self.state.settings.spool_dir / other_ref).exists())
        self.assertTrue(path.exists())
        self.assertIsNotNone(accepted)
