"""Model calls and deterministic construction of a complete prepared revision."""

from __future__ import annotations

from typing import Literal
from uuid import uuid5

import asyncio
from rag.runtime.events import _span
from rag.contracts import StepError
from rag.ingestion._dependencies import _Dependencies
from rag.ingestion._execution import _Boundary, _Budget
from rag.ingestion._settings import _Settings
from rag.ingestion._topics import _Topics
from rag.ingestion.contracts import DocumentBatch
from rag.model_gateway.contracts import EmbeddingResult
from rag.runtime.postgresql.ingestion import ReservedRevision
from rag.runtime.qdrant import ChunkPoint, TopicPoint
from rag.runtime.upload_spool import PreparedRevision


class _Preparation:
    def __init__(self, settings: _Settings, deps: _Dependencies,
                 budget: _Budget) -> None:
        self.settings = settings
        self.deps = deps
        self.budget = budget

    async def _embed(self, texts: list[str], purpose: Literal["query", "document"]) -> list[list[float]]:
        if any(len(text) > self.settings.embedding_input_characters for text in texts):
            raise StepError("embedding_input_too_large", False)
        vectors: list[list[float]] = []
        for start in range(0, len(texts), self.settings.embedding_batch_size):
            portion = texts[start:start + self.settings.embedding_batch_size]
            timeout = self.budget.remaining(self.settings.embedding_timeout_s)
            # embed_document/embed_query → parent step; Model Gateway adds embed_texts below it.
            # Profile/shape validation belongs to this step, so invalid output is logged as failed.
            with _span("embed_" + purpose):
                try:
                    async with asyncio.timeout(timeout):
                        raw = await self.deps.models.embed_texts(portion, purpose, timeout_s=timeout)
                except TimeoutError:
                    raise StepError("model_timeout", True) from None
                result = _Boundary.validate(raw, EmbeddingResult, "model_invalid_response")
                if result.profile_id != self.settings.embedding_profile:
                    raise StepError("model_profile_mismatch", False)
                if (len(result.vectors) != len(portion)
                        or any(len(vector) != self.settings.embedding_dimensions for vector in result.vectors)):
                    raise StepError("model_invalid_response", False)
            vectors.extend(result.vectors)
        return vectors

    async def build(self, batch: DocumentBatch, revision: ReservedRevision) -> PreparedRevision:
        # ├── embed_document → dense vectors of the accepted chunks.
        vectors = await self._embed([chunk.text for chunk in batch.chunks], "document")
        # ├── generate_topic_questions → generate/validate examples from every portion.
        questions = await _Topics(self.settings, self.deps, self.budget).generate(batch)
        # └── embed_query → encode examples with the same purpose used by Guardrails.
        topic_vectors = await self._embed(questions, "query")
        points = [
            ChunkPoint(
                chunk_id=uuid5(revision.revision_id, str(number)),
                document_id=revision.document_id, revision_id=revision.revision_id,
                source_name=batch.source.filename, source_url=batch.source.source_url,
                text=chunk.text, locator=chunk.locator, images=chunk.images,
                chunk_no=number, document_version=revision.document_version,
                access_level=batch.source.access_level, dense=vector,
            )
            for number, (chunk, vector) in enumerate(zip(batch.chunks, vectors, strict=True))
        ]
        topic_points = [
            TopicPoint(
                point_id=uuid5(revision.revision_id, "question:" + str(number)),
                revision_id=revision.revision_id, question_no=number,
                text=question, dense=vector,
            )
            for number, (question, vector) in enumerate(zip(questions, topic_vectors, strict=True))
        ]
        return PreparedRevision(
            revision_id=revision.revision_id,
            preparation_profile=self.settings.preparation_profile,
            embedding_profile=self.settings.embedding_profile,
            points=points, topic_points=topic_points,
        )
