"""Repository transitions and migration planning without a running database."""

from contextlib import ExitStack, asynccontextmanager
from datetime import UTC, datetime, timedelta
from io import StringIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch
from uuid import uuid4

from alembic.migration import MigrationContext
from alembic.operations import Operations

from rag.contracts import StepError, StorageError
from rag.ingestion.contracts import IngestionAttempt, UploadSource
from rag.runtime.postgresql import ingestion as pg
from rag.runtime._migrations.versions import ingestion_documents as migration


class IngestionDatabaseTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.revision, self.document = uuid4(), uuid4()
        self.source = UploadSource(filename="Guide.docx", access_level="employees")
        self.now = datetime(2026, 11, 14, tzinfo=UTC)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)

        @asynccontextmanager
        async def transaction():
            yield object()

        self.stack.enter_context(patch.object(pg, "_transaction", transaction))
        self.one = self.stack.enter_context(patch.object(pg, "_one", new=AsyncMock()))
        self.execute = self.stack.enter_context(patch.object(pg, "_execute", new=AsyncMock()))
        self.lock = self.stack.enter_context(patch.object(pg, "_lock", new=AsyncMock()))
        self.outbox = self.stack.enter_context(patch.object(pg, "_outbox", new=AsyncMock()))
        self.stack.enter_context(patch.object(pg, "_now", new=AsyncMock(return_value=self.now)))
        self.stack.enter_context(patch.object(pg, "_get", return_value=SimpleNamespace(
            settings=SimpleNamespace(ingestion_timeout_s=120))))
        self.watchdog = self.stack.enter_context(patch.object(pg._watchdog, "_claimed"))

    async def _register(self, document_id=None):
        return await pg.register_revision(self.revision, document_id,
                                          f"uploads/{self.revision}", self.source, [])

    def _row(self, **updates):
        row = dict(document_id=self.document, revision_id=self.revision,
                   staging_ref=f"uploads/{self.revision}", source_name=self.source.filename,
                   access_level=self.source.access_level, image_ids=[], state="queued",
                   qdrant_write_pending=False, resume_at=None, attempt_no=0, revision_no=2,
                   document_version=None, base_version=None,
                   expected_chunk_count=None, expected_topic_count=None)
        row.update(updates)
        return row

    async def test_new_document_gets_registry_row_access_metadata_and_one_outbox(self):
        self.one.side_effect = [None, None, {"number": None}]
        accepted = await self._register()
        self.assertEqual(accepted.job_id, self.revision)
        statements = [call.args[1] for call in self.execute.await_args_list]
        self.assertEqual([statement.table.name for statement in statements],
                         ["documents", "document_revisions"])
        self.assertEqual(statements[0].compile().params["document_id"], accepted.document_id)
        metadata = statements[1].compile().params
        self.assertEqual((metadata["source_name"], metadata["access_level"], metadata["revision_no"]),
                         ("Guide.docx", "employees", 1))
        self.outbox.assert_awaited_once()
        self.assertEqual(self.outbox.await_args.args[1:], ("ingestion.jobs", self.revision))
        self.assertIn(("document_name", "Guide.docx"),
                      [call.args[1:] for call in self.lock.await_args_list])

    async def test_same_exact_name_updates_existing_document_without_new_registry(self):
        self.one.side_effect = [None, {"document_id": self.document, "source_name": "Guide.docx"},
                               {"number": 4}]
        accepted = await self._register()
        self.assertEqual(accepted.document_id, self.document)
        self.execute.assert_awaited_once()
        params = self.execute.await_args.args[1].compile().params
        self.assertEqual((params["document_id"], params["revision_no"]), (self.document, 5))

    async def test_unknown_explicit_document_has_no_insert_or_outbox(self):
        self.one.side_effect = [None, None]
        self.assertIsNone(await self._register(self.document))
        self.execute.assert_not_awaited()
        self.outbox.assert_not_awaited()

    async def test_explicit_document_cannot_be_renamed_through_upload(self):
        self.one.side_effect = [None, {"document_id": self.document, "source_name": "Other.docx"}]
        with self.assertRaisesRegex(StepError, "document_name_conflict"):
            await self._register(self.document)
        self.execute.assert_not_awaited()

    async def test_identical_revision_is_idempotent_and_changed_access_conflicts(self):
        self.one.side_effect = [self._row()]
        self.assertEqual((await self._register()).document_id, self.document)
        self.execute.assert_not_awaited()
        self.outbox.assert_not_awaited()
        self.one.side_effect = [self._row(access_level="operators")]
        with self.assertRaisesRegex(StepError, "revision_conflict"):
            await self._register()

    async def test_fresh_claim_has_no_assigned_document_version(self):
        self.one.side_effect = [self._row(), None, None]
        job = await pg.claim_ingestion_job(self.revision)
        self.assertIsNone(job.document_version)
        self.assertEqual(job.attempt.attempt_no, 1)
        self.assertEqual(job.attempt.deadline_at, self.now + timedelta(seconds=120))
        self.execute.assert_awaited_once()
        self.watchdog.assert_called_once_with(job.attempt)

    async def test_retry_preserves_reservation_base_and_both_expected_counts(self):
        self.one.side_effect = [self._row(attempt_no=2, document_version=3, base_version=2,
                                         expected_chunk_count=7, expected_topic_count=4), None, None]
        job = await pg.claim_ingestion_job(self.revision)
        self.assertEqual((job.document_version, job.base_version,
                          job.expected_chunk_count, job.expected_topic_count), (3, 2, 7, 4))
        self.assertEqual(job.attempt.attempt_no, 3)
        params = self.execute.await_args.args[1].compile().params
        self.assertNotIn("document_version", params)
        self.assertNotIn("base_version", params)

    async def test_invalid_stored_counts_do_not_advance_attempt(self):
        self.one.side_effect = [self._row(expected_chunk_count=1, expected_topic_count=1), None, None]
        with self.assertRaises(StorageError):
            await pg.claim_ingestion_job(self.revision)
        self.execute.assert_not_awaited()
        self.watchdog.assert_not_called()

    async def test_pending_deadline_and_other_worker_block_claim(self):
        for row in (self._row(qdrant_write_pending=True),
                    self._row(resume_at=self.now + timedelta(seconds=5)),
                    self._row(state="published")):
            self.one.side_effect = [row]
            self.assertIsNone(await pg.claim_ingestion_job(self.revision))
        self.one.side_effect = [self._row(), None, {"revision_id": uuid4()}]
        self.assertIsNone(await pg.claim_ingestion_job(self.revision))
        self.execute.assert_not_awaited()


    def _attempt(self):
        return IngestionAttempt(job_id=self.revision, attempt_no=1,
                                deadline_at=self.now + timedelta(seconds=120))

    def _indexing_row(self, **changes):
        return self._row(state="indexing", attempt_no=1,
                         resume_at=self._attempt().deadline_at, **changes)

    async def test_read_version_zero_is_a_result(self):
        self.one.return_value = {"document_id": self.document, "current_version": 0}
        result = await pg.read_document_version(self.source.filename)
        self.assertEqual(result.document_version, 0)
        for row in (None, {"current_version": None}):
            self.one.return_value = row
            self.assertIsNone(await pg.read_document_version(self.source.filename))

    async def test_reserve_zero_and_repeat_are_idempotent(self):
        self.one.side_effect = [{"document_id": self.document}, {"current_version": None},
                                self._indexing_row(), None]
        result = await pg.reserve_document_version(self._attempt(), None)
        self.assertEqual(result.document_version, 0)
        self.execute.assert_awaited_once()
        self.execute.reset_mock()
        self.one.side_effect = [{"document_id": self.document}, {"current_version": None},
                                self._indexing_row(document_version=0), None]
        self.assertEqual(await pg.reserve_document_version(self._attempt(), None), result)
        self.execute.assert_not_awaited()

    async def test_reserve_checks_current_base_and_stored_reservation(self):
        for version, expected, retryable in ((None, None, True), (2, 0, False)):
            self.one.side_effect = [{"document_id": self.document}, {"current_version": 1},
                                    self._indexing_row(document_version=version, base_version=1), None]
            with self.assertRaises(StepError) as caught:
                await pg.reserve_document_version(self._attempt(), expected)
            self.assertEqual(caught.exception.code, "document_version_conflict")
            self.assertEqual(caught.exception.retryable, retryable)
        self.execute.assert_not_awaited()

    async def test_lost_or_pending_attempt_cannot_reserve(self):
        for row in (None, self._indexing_row(qdrant_write_pending=True), self._row()):
            self.one.side_effect = [{"document_id": self.document}, {"current_version": None}, row]
            self.assertIsNone(await pg.reserve_document_version(self._attempt(), None))
        self.execute.assert_not_awaited()

    async def test_begin_requires_reserved_version_before_sql_write(self):
        self.one.return_value = self._indexing_row()
        with self.assertRaisesRegex(StepError, "document_version_missing"):
            await pg.begin_revision_write(self._attempt(), 1, 1)
        self.execute.assert_not_awaited()

    async def test_publish_updates_document_and_corpus_in_same_transaction(self):
        self.stack.enter_context(patch.object(pg, "_corpus", new=AsyncMock(return_value={"generation": 7})))
        self.stack.enter_context(patch.object(pg, "_wake_ingestion", new=AsyncMock()))
        self.one.side_effect = [{"document_id": self.document}, self._indexing_row(
            document_version=1, base_version=0, expected_chunk_count=2, expected_topic_count=1,
            qdrant_write_pending=True), {"current_version": 0}]
        self.assertEqual(await pg.publish_revision(self._attempt()), 8)
        registry = self.execute.await_args_list[-1].args[1]
        self.assertEqual(registry.table.name, "documents")
        self.assertEqual(registry.compile().params["current_version"], 1)
        self.assertEqual(registry.compile().params["current_revision_id"], self.revision)

    async def test_stale_publish_base_does_not_retire_current_revision(self):
        self.stack.enter_context(patch.object(pg, "_corpus", new=AsyncMock(return_value={"generation": 7})))
        self.one.side_effect = [{"document_id": self.document}, self._indexing_row(
            document_version=1, base_version=0, expected_chunk_count=2, expected_topic_count=1,
            qdrant_write_pending=True), {"current_version": 1}]
        with self.assertRaisesRegex(StepError, "document_version_conflict"):
            await pg.publish_revision(self._attempt())
        self.execute.assert_not_awaited()


class IngestionMigrationTests(unittest.TestCase):
    def _legacy(self, **updates):
        row = dict(revision_id=uuid4(), document_id=uuid4(), source_name="Guide.docx",
                   revision_no=1, state="queued", visible_from=None, expected_chunk_count=None)
        row.update(updates)
        return row

    def test_empty_database_requires_no_legacy_access_policy(self):
        self.assertEqual(migration._legacy_plan([], None), ([], []))

    def test_existing_data_requires_explicit_access_label(self):
        for level in (None, "", " "):
            with self.subTest(level=level), self.assertRaisesRegex(RuntimeError, "explicit"):
                migration._legacy_plan([self._legacy()], level)

    def test_existing_ids_publication_versions_and_pending_base_are_preserved(self):
        document = uuid4()
        rows = [self._legacy(document_id=document, revision_no=1, state="failed"),
                self._legacy(document_id=document, revision_no=2, state="retired", visible_from=5,
                             expected_chunk_count=2),
                self._legacy(document_id=document, revision_no=3, state="failed", expected_chunk_count=2),
                self._legacy(document_id=document, revision_no=4, state="published", visible_from=9,
                             expected_chunk_count=3),
                self._legacy(document_id=document, revision_no=5, state="indexing", expected_chunk_count=4)]
        documents, revisions = migration._legacy_plan(rows, "legacy-employees")
        self.assertEqual(documents, [dict(document_id=document, source_name="Guide.docx",
                                         current_revision_id=rows[3]["revision_id"], current_version=1)])
        self.assertEqual([r["revision_id"] for r in revisions], [r["revision_id"] for r in rows])
        self.assertEqual([(r["document_version"], r["base_version"]) for r in revisions],
                         [(None, None), (0, None), (1, 0), (1, 0), (2, 1)])
        self.assertEqual({r["access_level"] for r in revisions}, {"legacy-employees"})

    def test_name_collision_or_rename_is_not_silently_merged(self):
        original = self._legacy()
        for other in (self._legacy(), self._legacy(document_id=original["document_id"], source_name="Renamed")):
            with self.subTest(other=other), self.assertRaises(RuntimeError):
                migration._legacy_plan([original, other], "employees")

    def test_postgresql_schema_has_constraints_foreign_keys_and_no_default_access(self):
        output = StringIO()
        context = MigrationContext.configure(dialect_name="postgresql", opts={
            "as_sql": True, "output_buffer": output,
        })
        operations = Operations(context)
        connection = Mock()
        connection.execute.return_value.mappings.return_value.all.return_value = []
        with patch.object(migration, "op", operations), \
                patch.object(operations, "get_bind", return_value=connection), \
                patch.object(operations, "get_context", return_value=SimpleNamespace(
                    config=SimpleNamespace(attributes={}))):
            migration.upgrade()
        sql = output.getvalue()
        for fragment in ("CREATE TABLE documents", 'COLLATE "C"', "UNIQUE (source_name)",
                         "revisions_counts_require_version", "revisions_reserved_version",
                         "FOREIGN KEY(document_id)", "FOREIGN KEY(current_revision_id)",
                         "ALTER COLUMN access_level SET NOT NULL"):
            self.assertIn(fragment, sql)
        self.assertNotIn("DEFAULT 'employees'", sql)

    def test_failed_preflight_emits_no_ddl(self):
        connection = Mock()
        connection.execute.return_value.mappings.return_value.all.return_value = [self._legacy()]
        with patch.object(migration.op, "get_bind", return_value=connection), \
                patch.object(migration.op, "get_context", return_value=SimpleNamespace(
                    config=SimpleNamespace(attributes={}) )), \
                patch.object(migration.op, "create_table") as create, \
                patch.dict("os.environ", {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "explicit"):
                migration.upgrade()
        create.assert_not_called()
