"""Source URL survives Qdrant payload, citations, cached JSON and Telegram links."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4, uuid5

from qdrant_client import models

from rag.contracts import GeneratedAnswer, RagResult
from rag.rag_api.contracts import ClaimedDelivery, DeliveryAttempt
from rag.rag_api.entrypoints import build_delivery_parts
from rag.rag_core.entrypoints import _result
from rag.runtime import _qdrant, _state, qdrant
from rag.runtime.redis import CachedAnswer


class SourceUrlFlowTests(unittest.IsolatedAsyncioTestCase):
    async def test_point_url_is_used_by_new_and_cached_answers(self):
        url = "https://docs.example/manual?revision=abc&view=1#section"
        revision, document = uuid4(), uuid4()
        point = qdrant.ChunkPoint(
            chunk_id=uuid5(revision, "0"), document_id=document, revision_id=revision,
            source_name="manual.md", source_url=url, text="Документ подтверждает ответ.",
            chunk_no=0, document_version=0, access_level="employees", dense=[0.1, 0.2],
        )
        client = SimpleNamespace(upsert=AsyncMock(return_value=SimpleNamespace(
            status=models.UpdateStatus.COMPLETED)), query_points=AsyncMock())
        state = SimpleNamespace(qdrant=client, settings=SimpleNamespace(
            io_timeout_s=2, embedding_dimensions=2, batch_size=10,
            chunks_collection="chunks", search_mode="dense"))
        with patch.object(_state, "_current", state), \
                patch.object(_qdrant, "_check", new=AsyncMock()), \
                patch.object(_qdrant, "_verify", new=AsyncMock(return_value=True)):
            await qdrant.write_revision_points(revision, [point])
            stored = client.upsert.await_args.kwargs["points"][0]
            self.assertEqual(stored.payload["source_url"], url)
            client.query_points.return_value = SimpleNamespace(points=[SimpleNamespace(
                id=stored.id, payload=stored.payload, score=0.9)])
            hits = await qdrant.find_chunks("Вопрос", [0.1, 0.2], frozenset({revision}), 10)

        generated = GeneratedAnswer(kind="answered", answer_text="Ответ по документу.",
            cited_chunk_ids=[point.chunk_id], cited_image_ids=[])
        result = _result(generated, [hit.fragment for hit in hits])
        self.assertEqual(result.sources[0].source_url, url)
        cached = CachedAnswer(corpus_generation=42, **result.model_dump())
        restored = CachedAnswer.model_validate_json(cached.model_dump_json())
        cached_result = RagResult.model_validate(restored.model_dump(exclude={"corpus_generation"}))
        for answer in (result, cached_result):
            delivery = ClaimedDelivery(
                attempt=DeliveryAttempt(delivery_id=2, attempt_no=1,
                    deadline_at=datetime.now(timezone.utc) + timedelta(minutes=1)),
                user_id=7, chat_id=7, request_id=1, purpose="answer", content=answer)
            parts = build_delivery_parts(delivery, "https://bot.example")
            self.assertEqual([entity.url for entity in parts[0].entities
                              if entity.type == "text_link"], [url])
