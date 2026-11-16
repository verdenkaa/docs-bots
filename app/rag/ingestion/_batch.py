"""Validation of a whole document and of its immutable prepared snapshot."""

from rag.contracts import StepError
from rag.ingestion.contracts import ClaimedIngestionJob, DocumentBatch
from rag.runtime.postgresql.ingestion import ReservedRevision
from rag.runtime.upload_spool import PreparedRevision


class _BatchChecks:
    @staticmethod
    def check_limits(batch: DocumentBatch, max_chunks: int, max_characters: int) -> None:
        if len(batch.chunks) > max_chunks:
            raise StepError("document_too_large", False)
        if sum(len(chunk.text) for chunk in batch.chunks) > max_characters:
            raise StepError("document_too_large", False)

    @staticmethod
    def check_reservation(job: ClaimedIngestionJob, revision: ReservedRevision,
                          expected_base: int | None) -> None:
        if (revision.revision_id != job.attempt.job_id
                or revision.document_id != job.document_id
                or revision.base_version != expected_base):
            raise StepError("revision_identity_mismatch", False)
        if (job.document_version is not None
                and revision.document_version != job.document_version):
            raise StepError("document_version_conflict", False)

    @staticmethod
    def check_snapshot(prepared: PreparedRevision, job: ClaimedIngestionJob,
                       batch: DocumentBatch, revision: ReservedRevision,
                       preparation_profile: str, embedding_profile: str,
                       dimensions: int) -> None:
        if prepared.revision_id != revision.revision_id:
            raise StepError("revision_identity_mismatch", False)
        if (prepared.preparation_profile != preparation_profile
                or prepared.embedding_profile != embedding_profile):
            raise StepError("model_profile_mismatch", False)
        if len(prepared.points) != len(batch.chunks):
            raise StepError("prepared_revision_mismatch", False)
        if (job.expected_chunk_count is not None
                and (len(prepared.points) != job.expected_chunk_count
                     or len(prepared.topic_points) != job.expected_topic_count)):
            raise StepError("prepared_revision_mismatch", False)
        for point, chunk in zip(prepared.points, batch.chunks, strict=True):
            if (point.document_id != job.document_id
                    or point.document_version != revision.document_version
                    or point.access_level != batch.source.access_level
                    or point.source_name != batch.source.filename
                    or point.source_url != batch.source.source_url
                    or point.text != chunk.text
                    or point.locator != chunk.locator
                    or point.images != chunk.images):
                raise StepError("prepared_revision_mismatch", False)
        if any(len(point.dense) != dimensions
               for point in [*prepared.points, *prepared.topic_points]):
            raise StepError("model_profile_mismatch", False)
