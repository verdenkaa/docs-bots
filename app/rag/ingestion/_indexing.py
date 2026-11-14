"""Document-level ingestion state machine over the existing adapter contracts."""

from __future__ import annotations

import asyncio
from datetime import timedelta

from rag.contracts import ConsumeDecision, StepError, StorageError
from rag.dto import PositiveId
from rag.ingestion._batch import _BatchChecks
from rag.ingestion._dependencies import _Dependencies
from rag.ingestion._execution import (
    _AttemptExpired, _Boundary, _Budget, _DatabaseUnknown, _LostAttempt, _Trace,
)
from rag.ingestion._maintenance import _Maintenance
from rag.ingestion._preparation import _Preparation
from rag.ingestion._settings import _Settings
from rag.ingestion.contracts import ClaimedIngestionJob, DocumentBatch, JobRef
from rag.runtime.postgresql.ingestion import DocumentVersion, ReservedRevision
from rag.runtime.upload_spool import PreparedRevision


class _Indexer:
    def __init__(self, settings: _Settings, deps: _Dependencies) -> None:
        self.settings = settings
        self.deps = deps
        self.job: ClaimedIngestionJob | None = None
        self.writing_started = False
        self.trace: _Trace
        self.budget: _Budget

    async def _db(self, name, function, annotation, *args):
        try:
            raw = await self.trace.call(name, self.budget, self.settings.step_timeout_s,
                                        function, *args)
        except (StorageError, TimeoutError):
            raise _DatabaseUnknown from None
        try:
            return _Boundary.validate(raw, annotation, "database_invalid_response")
        except StepError:
            raise _DatabaseUnknown from None

    async def _spool(self, name, function, annotation, *args):
        try:
            raw = await self.trace.call(name, self.budget, self.settings.step_timeout_s,
                                        function, *args)
        except (StorageError, TimeoutError):
            raise StepError("staging_unavailable", True) from None
        return _Boundary.validate(raw, annotation, "staging_invalid_data")

    async def _reserve(self, batch: DocumentBatch) -> ReservedRevision:
        job = self.job
        assert job is not None
        if job.document_version is None:
            current = await self._db("read_document_version", self.deps.jobs.read_document_version,
                                     DocumentVersion | None, batch.source.filename)
            if current is not None and current.document_id != job.document_id:
                raise StepError("document_identity_mismatch", False)
            base = None if current is None else current.document_version
        else:
            base = job.base_version
        revision = await self._db(
            "reserve_document_version", self.deps.jobs.reserve_document_version,
            ReservedRevision | None, job.attempt, base,
        )
        if revision is None:
            raise _LostAttempt
        _BatchChecks.check_reservation(job, revision, base)
        self.trace.fields["document_version"] = revision.document_version
        return revision

    def _check_snapshot(self, prepared: PreparedRevision, batch: DocumentBatch,
                        revision: ReservedRevision) -> None:
        assert self.job is not None
        _BatchChecks.check_snapshot(
            prepared, self.job, batch, revision, self.settings.preparation_profile,
            self.settings.embedding_profile, self.settings.embedding_dimensions,
        )
        if len(prepared.topic_points) > self.settings.max_topic_questions:
            raise StepError("too_many_topic_questions", False)

    async def _prepare(self, batch: DocumentBatch, revision: ReservedRevision) -> PreparedRevision:
        job = self.job
        assert job is not None
        prepared = await self._spool(
            "load_prepared_revision", self.deps.spool.load_prepared_revision,
            PreparedRevision | None, job.staging_ref,
        )
        if prepared is not None:
            self._check_snapshot(prepared, batch, revision)
            return prepared
        if job.expected_chunk_count is not None:
            raise StepError("prepared_revision_missing", False)
        prepared = await _Preparation(self.settings, self.deps, self.trace, self.budget).build(batch, revision)
        self._check_snapshot(prepared, batch, revision)
        try:
            raw = await self.trace.call(
                "store_prepared_revision", self.budget, self.settings.step_timeout_s,
                self.deps.spool.store_prepared_revision, job.staging_ref, prepared,
            )
            _Boundary.validate(raw, type(None), "staging_invalid_data")
        except (StorageError, TimeoutError):
            restored = await self._spool(
                "confirm_prepared_revision", self.deps.spool.load_prepared_revision,
                PreparedRevision | None, job.staging_ref,
            )
            if restored is None:
                raise StepError("prepared_revision_unconfirmed", True) from None
            self._check_snapshot(restored, batch, revision)
            if restored != prepared:
                raise StepError("prepared_revision_conflict", False) from None
            prepared = restored
        return prepared

    async def _index(self, name, function, annotation, *args):
        try:
            raw = await self.trace.call(name, self.budget, self.settings.index_timeout_s,
                                        function, *args)
        except TimeoutError:
            raise StepError("index_timeout", True) from None
        return _Boundary.validate(raw, annotation, "index_invalid_response")

    async def _run(self, event: JobRef) -> str:
        self.job = await self._db("claim_ingestion_job", self.deps.jobs.claim_ingestion_job,
                                  ClaimedIngestionJob | None, event.job_id)
        job = self.job
        if job is None:
            return "idle"
        if job.attempt.job_id != event.job_id:
            # Do not mutate a different job if an adapter returns the wrong ID.
            self.job = None
            raise _DatabaseUnknown
        self.budget = _Budget.from_deadline(job.attempt.deadline_at, self.deps)
        self.trace.fields.update(attempt_no=job.attempt.attempt_no, document_id=str(job.document_id))
        if job.attempt.attempt_no > self.settings.max_attempts:
            raise StepError("ingestion_attempts_exhausted", False)
        batch = await self._spool("load_document_batch", self.deps.spool.load_document_batch,
                                   DocumentBatch | None, job.staging_ref)
        if batch is None:
            raise StepError("document_batch_missing", False)
        _BatchChecks.check_limits(batch, self.settings.max_chunks, self.settings.max_document_characters)
        revision = await self._reserve(batch)
        prepared = await self._prepare(batch, revision)
        allowed = await self._db("begin_revision_write", self.deps.jobs.begin_revision_write,
                                 bool, job.attempt, len(prepared.points), len(prepared.topic_points))
        if not allowed:
            raise _LostAttempt
        self.writing_started = True
        await self._index("write_revision_points", self.deps.index.write_revision_points,
                          type(None), revision.revision_id, prepared.points)
        await self._index("write_topic_points", self.deps.index.write_topic_points,
                          type(None), revision.revision_id, prepared.topic_points)
        if not await self._index("verify_revision_points", self.deps.index.verify_revision_points,
                                 bool, revision.revision_id, len(prepared.points)):
            raise StepError("revision_points_mismatch", True)
        if not await self._index("verify_topic_points", self.deps.index.verify_topic_points,
                                 bool, revision.revision_id, len(prepared.topic_points)):
            raise StepError("topic_points_mismatch", True)
        generation = await self._db("publish_revision", self.deps.jobs.publish_revision,
                                    PositiveId | None, job.attempt)
        if generation is None:
            raise _LostAttempt
        self.trace.fields["corpus_generation"] = generation
        return "published"

    async def _persist_error(self, error: StepError) -> str:
        job = self.job
        if job is None:
            raise _DatabaseUnknown
        if self.writing_started:
            # begin_revision_write was confirmed and no successful publication
            # followed. Only the documented reconciliation may clear pending.
            return "reconciliation_required"
        if error.retryable and job.attempt.attempt_no < self.settings.max_attempts:
            delay = min(self.settings.retry_max_s,
                        self.settings.retry_initial_s * 2 ** min(job.attempt.attempt_no - 1, 30))
            applied = await self._db(
                "schedule_ingestion_retry", self.deps.jobs.schedule_ingestion_retry,
                bool, job.attempt, self.deps.now() + timedelta(seconds=delay),
            )
            outcome = "retry_scheduled"
        else:
            applied = await self._db("fail_ingestion_job", self.deps.jobs.fail_ingestion_job,
                                     bool, job.attempt)
            outcome = "failed"
        if not applied:
            raise _LostAttempt
        return outcome

    async def handle(self, event: JobRef) -> ConsumeDecision:
        self.job = None
        self.writing_started = False
        self.trace = _Trace(str(event.job_id), self.deps)
        self.budget = _Budget.for_seconds(self.settings.step_timeout_s, self.deps)
        decision: ConsumeDecision | None = None
        outcome = "unexpected_error"
        error_code: str | None = None
        try:
            try:
                try:
                    outcome = await self._run(event)
                except StepError as error:
                    error_code = error.code
                    outcome = await self._persist_error(error)
                decision = "ack"
            except _DatabaseUnknown:
                decision, outcome, error_code = "retry_broker", "database_unknown", "storage_unconfirmed"
            except _AttemptExpired:
                # An expired attempt cannot persist a new retry/failure. Let a
                # redelivery/recovery verify the actual database state.
                decision, outcome, error_code = "retry_broker", "attempt_expired", "attempt_expired"
            except _LostAttempt:
                decision, outcome = "ack", "attempt_lost"
            if outcome in ("published", "failed", "idle"):
                errors = await _Maintenance(self.settings, self.deps, self.trace).run()
                if errors:
                    self.trace.fields["maintenance_error_codes"] = errors
            assert decision is not None
            return decision
        except asyncio.CancelledError:
            outcome, decision = "cancelled", None
            raise
        finally:
            self.trace.finish(outcome, decision, error_code)
