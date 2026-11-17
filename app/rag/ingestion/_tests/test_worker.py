"""Execute the production Worker against stateful in-memory dependency ports."""

import asyncio
from datetime import timedelta
import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4, uuid5

from rag.contracts import StepError, StorageError
from rag.ingestion._indexing import _Indexer
from rag.ingestion._tests._support import _FakeDependencies, _Fixtures
from rag.ingestion.contracts import JobRef
from rag.ingestion.worker import handle_ingestion_job
from rag.model_gateway.contracts import EmbeddingResult, ModelGenerationResult
from rag.runtime.postgresql.cleanup import StagingCleanup
from rag.runtime.postgresql.ingestion import DocumentVersion


class WorkerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.deps = _FakeDependencies()
        self.settings = _Fixtures.settings()
        self.event = JobRef(job_id=_Fixtures.revision)

    async def _run(self):
        return await _Indexer(self.settings, self.deps).handle(self.event)

    async def test_new_document_is_prepared_and_published_as_one_revision(self):
        self.assertEqual(await self._run(), "ack")
        self.assertEqual(self.deps.state, "published")
        self.assertFalse(self.deps.pending)
        self.assertEqual(self.deps.reservation.document_version, 0)
        for number, (point, chunk) in enumerate(zip(self.deps.chunk_points, self.deps.batch.chunks)):
            self.assertEqual(point.chunk_id, uuid5(_Fixtures.revision, str(number)))
            self.assertEqual((point.text, point.images), (chunk.text, chunk.images))
            self.assertEqual((point.source_name, point.access_level, point.document_version),
                             ("Сеть.docx", "employees", 0))
        self.assertEqual([name for name in self.deps.calls if name.startswith("write") or name.startswith("verify") or name == "publish"],
                         ["write_chunks", "write_topics", "verify_chunks", "verify_topics", "publish"])
        self.assertLess(self.deps.calls.index("store_prepared"), self.deps.calls.index("begin"))
        generated = [item for kind, item in self.deps.model_inputs if kind == "generate"]
        self.assertEqual(generated[0]["chunks"], [chunk.text for chunk in self.deps.batch.chunks])
        self.assertEqual(len(self.deps.prepared.topic_points), 2)

    async def test_published_zero_is_updated_to_one(self):
        self.deps.current = DocumentVersion(document_id=_Fixtures.document, document_version=0)
        await self._run()
        self.assertEqual(self.deps.reservation.document_version, 1)
        self.assertEqual(self.deps.reservation.base_version, 0)

    async def test_source_url_is_preserved_in_index_points_and_snapshot(self):
        url = "https://docs.example/network?revision=abc&view=1#section"
        self.deps.batch.source.source_url = url
        self.assertEqual(await self._run(), "ack")
        self.assertEqual(self.deps.state, "published")
        self.assertTrue(all(point.source_url == url for point in self.deps.chunk_points))
        self.assertTrue(all(point.source_url == url for point in self.deps.prepared.points))

    async def test_retry_refuses_a_snapshot_with_changed_source_url(self):
        self.deps.batch.source.source_url = "https://docs.example/network?revision=abc"
        self.deps.errors["write_chunks"] = StepError("qdrant_unavailable", True)
        await self._run()
        self.deps.pending = False
        for point in self.deps.prepared.points:
            point.source_url = "https://docs.example/other"
        self.deps.errors.clear()
        self.deps.calls.clear()
        self.assertEqual(await self._run(), "ack")
        self.assertIn("fail", self.deps.calls)
        self.assertNotIn("write_chunks", self.deps.calls)

    async def test_duplicate_after_publication_does_not_regenerate(self):
        await self._run()
        self.deps.calls.clear()
        self.assertEqual(await self._run(), "ack")
        self.assertNotIn("read_version", self.deps.calls)
        self.assertNotIn("generate", self.deps.calls)
        self.assertNotIn("write_chunks", self.deps.calls)

    async def test_retry_reuses_reservation_and_snapshot_after_reconciliation(self):
        self.deps.errors["write_topics"] = StepError("qdrant_unavailable", True)
        self.assertEqual(await self._run(), "ack")
        snapshot = self.deps.prepared.model_dump_json()
        # An operator has confirmed removal of old writes and cleared pending.
        self.deps.pending = False
        self.deps.errors.clear()
        self.deps.calls.clear()
        self.assertEqual(await self._run(), "ack")
        self.assertEqual(self.deps.prepared.model_dump_json(), snapshot)
        self.assertNotIn("read_version", self.deps.calls)
        self.assertNotIn("generate", self.deps.calls)
        self.assertNotIn("embed_document", self.deps.calls)
        self.assertEqual(self.deps.reservation.document_version, 0)

    async def test_postgres_uncertainty_never_means_new_document(self):
        self.deps.errors["read_version"] = StorageError("database unavailable")
        self.assertEqual(await self._run(), "retry_broker")
        self.assertNotIn("reserve", self.deps.calls)
        self.assertNotIn("fail", self.deps.calls)

    async def test_unknown_begin_commit_does_not_write_or_schedule(self):
        self.deps.errors["begin"] = StorageError("commit unknown")
        self.assertEqual(await self._run(), "retry_broker")
        for name in ("write_chunks", "retry", "fail"):
            self.assertNotIn(name, self.deps.calls)

    async def test_unknown_publication_commit_is_checked_on_redelivery(self):
        self.deps.publish_commit_unknown = True
        self.assertEqual(await self._run(), "retry_broker")
        self.deps.calls.clear()
        self.assertEqual(await self._run(), "ack")
        self.assertNotIn("write_chunks", self.deps.calls)

    async def test_unknown_snapshot_store_is_confirmed_before_writes(self):
        self.deps.store_commit_unknown = True
        self.assertEqual(await self._run(), "ack")
        self.assertEqual(self.deps.calls.count("load_prepared"), 2)
        self.assertLess(self.deps.calls.index("load_prepared", self.deps.calls.index("store_prepared")),
                        self.deps.calls.index("begin"))

    async def test_unconfirmed_snapshot_store_schedules_retry_without_index_write(self):
        self.deps.errors["store_prepared"] = StorageError("volume unavailable")
        self.assertEqual(await self._run(), "ack")
        self.assertIn("retry", self.deps.calls)
        self.assertNotIn("begin", self.deps.calls)

    async def test_transient_preparation_failure_persists_bounded_retry(self):
        self.deps.errors["embed_document"] = StepError("model_unavailable", True)
        self.assertEqual(await self._run(), "ack")
        self.assertEqual(self.deps.retry_at, self.deps.now() + timedelta(seconds=1))
        self.assertNotIn("begin", self.deps.calls)

    async def test_last_attempt_fails_instead_of_scheduling_again(self):
        data = self.deps.job.attempt.model_dump()
        data["attempt_no"] = self.settings.max_attempts
        self.deps._update_job(attempt=data)
        self.deps.errors["embed_document"] = StepError("model_unavailable", True)
        self.assertEqual(await self._run(), "ack")
        self.assertIn("fail", self.deps.calls)
        self.assertNotIn("retry", self.deps.calls)

    async def test_exhausted_attempt_is_finalized_before_loading_material(self):
        data = self.deps.job.attempt.model_dump()
        data["attempt_no"] = self.settings.max_attempts + 1
        self.deps._update_job(attempt=data)
        self.assertEqual(await self._run(), "ack")
        self.assertNotIn("load_batch", self.deps.calls)
        self.assertIn("fail", self.deps.calls)

    async def test_failure_after_begin_waits_for_reconciliation(self):
        self.deps.errors["write_chunks"] = StepError("qdrant_unavailable", True)
        self.assertEqual(await self._run(), "ack")
        self.assertTrue(self.deps.pending)
        self.assertTrue(self.deps.attempt_released)
        self.assertEqual(self.deps.calls[-1], "release_pending")
        for name in ("write_topics", "publish", "retry", "fail", "delete_points"):
            self.assertNotIn(name, self.deps.calls)
        self.assertEqual(self.deps.events[-1]["outcome"], "reconciliation_required")

    async def test_unknown_pending_release_commit_does_not_acknowledge_work(self):
        self.deps.errors["write_chunks"] = StepError("qdrant_unavailable", True)
        self.deps.errors["release_pending"] = StorageError("unknown commit")
        self.assertEqual(await self._run(), "retry_broker")
        self.assertTrue(self.deps.pending)
        for name in ("retry", "fail", "publish", "delete_points"):
            self.assertNotIn(name, self.deps.calls)

    async def test_stale_pending_release_does_not_claim_reconciliation_success(self):
        self.deps.errors["write_chunks"] = StepError("qdrant_unavailable", True)
        with patch.object(self.deps, "_release_pending_attempt", new=AsyncMock(return_value=False)):
            self.assertEqual(await self._run(), "ack")
        self.assertTrue(self.deps.pending)
        self.assertEqual(self.deps.events[-1]["outcome"], "attempt_lost")

    async def test_incomplete_index_cannot_be_published(self):
        with patch.object(self.deps, "verify_topic_points", new=AsyncMock(return_value=False)):
            self.assertEqual(await self._run(), "ack")
        self.assertTrue(self.deps.pending)
        self.assertNotIn("publish", self.deps.calls)

    async def test_lost_begin_right_stops_all_index_writes(self):
        with patch.object(self.deps, "begin_revision_write", new=AsyncMock(return_value=False)):
            self.assertEqual(await self._run(), "ack")
        self.assertNotIn("write_chunks", self.deps.calls)
        self.assertNotIn("fail", self.deps.calls)

    async def test_missing_batch_is_terminal_and_does_not_rechunk(self):
        self.deps.batch = None
        self.assertEqual(await self._run(), "ack")
        self.assertEqual(self.deps.state, "failed")
        self.assertNotIn("generate", self.deps.calls)

    async def test_missing_snapshot_after_an_index_write_is_not_regenerated(self):
        self.deps.errors["write_chunks"] = StepError("qdrant_unavailable", True)
        await self._run()
        self.deps.pending = False  # Operator reconciliation.
        self.deps.prepared = None
        self.deps.errors.clear()
        self.deps.calls.clear()
        self.assertEqual(await self._run(), "ack")
        self.assertIn("fail", self.deps.calls)
        self.assertNotIn("generate", self.deps.calls)

    async def test_changed_snapshot_content_is_rejected(self):
        self.deps.errors["write_chunks"] = StepError("qdrant_unavailable", True)
        await self._run()
        self.deps.pending = False
        self.deps.prepared.points[1].text = "Подменённое содержимое"
        self.deps.errors.clear()
        self.deps.calls.clear()
        self.assertEqual(await self._run(), "ack")
        self.assertIn("fail", self.deps.calls)
        self.assertNotIn("write_chunks", self.deps.calls)

    async def test_wrong_document_from_lookup_is_not_indexed(self):
        self.deps.current = DocumentVersion(document_id=uuid4(), document_version=0)
        self.assertEqual(await self._run(), "ack")
        self.assertNotIn("reserve", self.deps.calls)
        self.assertIn("fail", self.deps.calls)

    async def test_bad_model_shape_and_profile_are_terminal(self):
        for response in (
            EmbeddingResult(profile_id="wrong", vectors=[[0.1, 0.2]]),
            EmbeddingResult(profile_id="embedding-test", vectors=[]),
            EmbeddingResult(profile_id="embedding-test", vectors=[[0.1]]),
        ):
            with self.subTest(response=response):
                self.deps = _FakeDependencies()
                with patch.object(self.deps, "embed_texts", new=AsyncMock(return_value=response)):
                    self.assertEqual(await self._run(), "ack")
                self.assertIn("fail", self.deps.calls)
                self.assertNotIn("begin", self.deps.calls)

    async def test_cancelled_model_call_does_not_ack_or_finalize(self):
        self.deps.errors["embed_document"] = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self._run()
        self.assertNotIn("retry", self.deps.calls)
        self.assertNotIn("fail", self.deps.calls)
        self.assertNotIn("consume_decision", self.deps.events[-1])

    async def test_expired_attempt_does_not_start_external_steps(self):
        data = self.deps.job.attempt.model_dump()
        data["deadline_at"] = self.deps.now() - timedelta(seconds=1)
        self.deps._update_job(attempt=data)
        self.assertEqual(await self._run(), "retry_broker")
        self.assertEqual(self.deps.calls, ["claim"])

    async def test_model_timeout_persists_retry(self):
        async def slow(*args, **kwargs):
            await asyncio.sleep(0.1)
        self.settings = _Fixtures.settings(embedding_timeout_s=0.005)
        with patch.object(self.deps, "embed_texts", new=slow):
            self.assertEqual(await self._run(), "ack")
        self.assertIn("retry", self.deps.calls)

    async def test_cleanup_requires_postgres_permission_and_keeps_order(self):
        old_revision = uuid4()
        self.deps.staging_cleanup = [StagingCleanup(revision_id=old_revision, staging_ref="old/upload")]
        self.deps.revision_cleanup = [old_revision]
        self.assertEqual(await self._run(), "ack")
        self.assertLess(self.deps.calls.index("remove_staging"), self.deps.calls.index("clear_staging"))
        self.assertLess(self.deps.calls.index("claim_cleanup"), self.deps.calls.index("delete_points"))
        self.assertLess(self.deps.calls.index("delete_points"), self.deps.calls.index("finish_cleanup"))

    async def test_cleanup_error_does_not_fail_published_document(self):
        self.deps.errors["list_staging"] = StorageError("cleanup unavailable")
        self.assertEqual(await self._run(), "ack")
        self.assertEqual(self.deps.state, "published")
        self.assertIn("claim_cleanup", self.deps.calls)
        self.assertTrue(self.deps.events[-1]["maintenance_error_codes"])

    async def test_logs_do_not_contain_document_or_generated_text(self):
        await self._run()
        logs = json.dumps(self.deps.events, ensure_ascii=False)
        self.assertNotIn("Настройте сервер", logs)
        self.assertNotIn("Как настроить сеть?", logs)
        self.assertNotIn("Сеть.docx", logs)
        self.assertEqual([event["event"] for event in self.deps.events], ["started", "finished"])

    async def test_public_entrypoint_keeps_contract_and_uses_real_indexer(self):
        with patch("rag.ingestion.worker._Settings.from_environment", return_value=self.settings), \
             patch("rag.ingestion.worker._Dependencies", return_value=self.deps):
            self.assertEqual(await handle_ingestion_job(self.event), "ack")
        self.assertEqual(self.deps.state, "published")

    async def test_model_events_keep_job_and_broker_correlation_context(self):
        from rag.runtime.events import _context, _event_context

        original = self.deps.embed_texts

        async def embedded(*args, **kwargs):
            self.assertEqual(_context.get()["job_id"], str(self.event.job_id))
            self.assertEqual(_context.get()["revision_id"], str(self.event.job_id))
            self.assertEqual(_context.get()["route"], "ingestion.jobs")
            self.assertEqual(_context.get()["message_id"], "notice-1")
            return await original(*args, **kwargs)

        with patch("rag.ingestion.worker._Settings.from_environment", return_value=self.settings), \
             patch("rag.ingestion.worker._Dependencies", return_value=self.deps), \
             patch.object(self.deps, "embed_texts", new=embedded), \
             _event_context(route="ingestion.jobs", message_id="notice-1"):
            self.assertEqual(await handle_ingestion_job(self.event), "ack")
            self.assertNotIn("job_id", _context.get())

    async def test_mutated_model_output_is_revalidated(self):
        response = EmbeddingResult(profile_id="embedding-test", vectors=[[0.1, 0.2]])
        response.vectors[0][0] = float("nan")
        with patch.object(self.deps, "embed_texts", new=AsyncMock(return_value=response)):
            self.assertEqual(await self._run(), "ack")
        self.assertIn("fail", self.deps.calls)
        self.assertNotIn("begin", self.deps.calls)

    async def test_mutated_stored_batch_is_revalidated_before_models(self):
        self.deps.batch.chunks[0].images.clear()
        self.assertEqual(await self._run(), "ack")
        self.assertIn("fail", self.deps.calls)
        self.assertNotIn("embed_document", self.deps.calls)

    async def test_unknown_retry_commit_returns_retry_broker(self):
        self.deps.errors["embed_document"] = StepError("model_unavailable", True)
        self.deps.errors["retry"] = StorageError("retry commit unknown")
        self.assertEqual(await self._run(), "retry_broker")
        self.assertNotIn("fail", self.deps.calls)

    async def test_programming_error_is_not_converted_to_business_failure(self):
        self.deps.errors["generate"] = RuntimeError("private source text")
        with self.assertRaises(RuntimeError):
            await self._run()
        self.assertNotIn("fail", self.deps.calls)
        self.assertNotIn("retry", self.deps.calls)
        self.assertNotIn("private source text", json.dumps(self.deps.events))

    async def test_cancelled_index_write_preserves_pending_without_ack(self):
        self.deps.errors["write_chunks"] = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self._run()
        self.assertTrue(self.deps.pending)
        self.assertNotIn("fail", self.deps.calls)
        self.assertNotIn("retry", self.deps.calls)
        self.assertNotIn("consume_decision", self.deps.events[-1])
