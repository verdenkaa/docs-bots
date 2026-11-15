"""In-memory contract ports for exercising the real ingestion handler."""

from datetime import UTC, datetime, timedelta
import json
from uuid import UUID

from rag.contracts import ImageRef, StorageError
from rag.ingestion._dependencies import _Dependencies
from rag.ingestion._settings import _Settings
from rag.ingestion.contracts import (
    ClaimedIngestionJob, DocumentBatch, IngestionAttempt, PreparedChunk, UploadSource,
)
from rag.model_gateway.contracts import EmbeddingResult, ModelGenerationResult
from rag.runtime.postgresql.ingestion import ReservedRevision
from rag.runtime.upload_spool import PreparedRevision


class _Fixtures:
    revision = UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")
    document = UUID("43f95f66-7343-4cbc-beb5-19fd36a8ca77")
    image = ImageRef(image_id=UUID("e1246aa5-3bba-4a74-8445-60b6271c0551"), description="Схема сети")

    @classmethod
    def settings(cls, **overrides):
        values = dict(
            embedding_profile="embedding-test", generation_profile="generation-test",
            preparation_profile="preparation-test-v1", embedding_dimensions=2,
            embedding_input_characters=4000, topic_input_characters=4000,
            generation_max_output_tokens=128, embedding_batch_size=1,
        )
        values.update(overrides)
        return _Settings(**values)

    @classmethod
    def batch(cls):
        return DocumentBatch(
            source=UploadSource(filename="Сеть.docx", access_level="employees"),
            chunks=[
                PreparedChunk(text=f"Настройте сервер. ![Схема сети](image://{cls.image.image_id})",
                              locator=None, images=[cls.image]),
                PreparedChunk(text="Для подключения укажите порт 443.", locator=None),
            ],
        )


class _FakeDependencies(_Dependencies):
    def __init__(self):
        self.jobs = self.spool = self.models = self.index = self.cleanup = self
        self.calls = []
        self.errors = {}
        self.events = []
        self.frozen_now = datetime(2030, 1, 1, tzinfo=UTC)
        self.job = ClaimedIngestionJob(
            attempt=IngestionAttempt(job_id=_Fixtures.revision, attempt_no=1,
                                     deadline_at=self.now() + timedelta(minutes=5)),
            document_id=_Fixtures.document, staging_ref="uploads/test",
        )
        self.batch = _Fixtures.batch()
        self.current = None
        self.reservation = None
        self.prepared = None
        self.pending = False
        self.attempt_released = False
        self.state = "indexing"
        self.chunk_points = []
        self.topic_points = []
        self.verified_chunks = self.verified_topics = False
        self.retry_at = None
        self.store_commit_unknown = False
        self.publish_commit_unknown = False
        self.generated_responses = []
        self.model_inputs = []
        self.staging_cleanup = []
        self.revision_cleanup = []

    def now(self):
        return self.frozen_now

    def emit(self, component, operation, event, run_id, **fields):
        self.events.append(dict(component=component, operation=operation,
                                event=event, run_id=run_id, **fields))

    def _enter(self, name):
        self.calls.append(name)
        if name in self.errors:
            raise self.errors[name]

    def _update_job(self, **values):
        data = self.job.model_dump()
        data.update(values)
        self.job = ClaimedIngestionJob.model_validate(data)

    async def claim_ingestion_job(self, job_id):
        self._enter("claim")
        if self.state in ("published", "failed") or self.pending:
            return None
        return self.job

    async def load_document_batch(self, staging_ref):
        self._enter("load_batch")
        return self.batch

    async def read_document_version(self, source_name):
        self._enter("read_version")
        return self.current

    async def reserve_document_version(self, attempt, expected_version):
        self._enter("reserve")
        if self.reservation is None:
            self.reservation = ReservedRevision(
                revision_id=attempt.job_id, document_id=self.job.document_id,
                document_version=0 if expected_version is None else expected_version + 1,
                base_version=expected_version,
            )
            self._update_job(document_version=self.reservation.document_version,
                             base_version=self.reservation.base_version)
        return self.reservation

    async def load_prepared_revision(self, staging_ref):
        self._enter("load_prepared")
        return self.prepared

    async def store_prepared_revision(self, staging_ref, prepared):
        self._enter("store_prepared")
        self.prepared = PreparedRevision.model_validate_json(prepared.model_dump_json())
        if self.store_commit_unknown:
            raise StorageError("lost acknowledgement")

    async def embed_texts(self, texts, purpose, *, timeout_s):
        self._enter("embed_" + purpose)
        self.model_inputs.append((purpose, list(texts)))
        return EmbeddingResult(profile_id="embedding-test", vectors=[[float(len(text)), 0.5] for text in texts])

    async def generate(self, messages, options, *, timeout_s):
        self._enter("generate")
        self.model_inputs.append(("generate", json.loads(messages[1].content)))
        text = (self.generated_responses.pop(0) if self.generated_responses
                else '{"questions":["Как настроить сеть?","Какой порт указать?"]}')
        return ModelGenerationResult(profile_id="generation-test", text=text,
                                     finish_reason="completed", usage=None)

    async def begin_revision_write(self, attempt, chunk_count, topic_count):
        self._enter("begin")
        assert self.prepared is not None and self.reservation is not None
        self.pending = True
        self._update_job(expected_chunk_count=chunk_count, expected_topic_count=topic_count)
        return True

    async def write_revision_points(self, revision_id, points):
        self._enter("write_chunks")
        assert self.pending and self.prepared is not None
        self.chunk_points = points

    async def write_topic_points(self, revision_id, points):
        self._enter("write_topics")
        assert self.pending
        self.topic_points = points

    async def verify_revision_points(self, revision_id, count):
        self._enter("verify_chunks")
        self.verified_chunks = len(self.chunk_points) == count
        return self.verified_chunks

    async def verify_topic_points(self, revision_id, count):
        self._enter("verify_topics")
        self.verified_topics = len(self.topic_points) == count
        return self.verified_topics

    async def publish_revision(self, attempt):
        self._enter("publish")
        assert self.pending and self.verified_chunks and self.verified_topics
        self.pending, self.state = False, "published"
        if self.publish_commit_unknown:
            raise StorageError("lost commit acknowledgement")
        return 42

    async def _release_pending_attempt(self, attempt):
        self._enter("release_pending")
        assert self.pending
        self.attempt_released = True
        return True

    async def schedule_ingestion_retry(self, attempt, retry_at):
        self._enter("retry")
        assert not self.pending
        self.retry_at = retry_at
        return True

    async def fail_ingestion_job(self, attempt):
        self._enter("fail")
        assert not self.pending
        self.state = "failed"
        return True

    async def list_finished_staging(self, limit):
        self._enter("list_staging")
        return self.staging_cleanup

    async def remove_staged_upload(self, staging_ref):
        self._enter("remove_staging")

    async def clear_staging_ref(self, revision_id, staging_ref):
        self._enter("clear_staging")
        return True

    async def claim_revision_cleanup(self, limit):
        self._enter("claim_cleanup")
        return self.revision_cleanup

    async def delete_revision_points(self, revision_id):
        self._enter("delete_points")

    async def finish_revision_cleanup(self, revision_id):
        self._enter("finish_cleanup")
        return True
