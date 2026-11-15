"""Search orchestration, revision isolation and ranking without external services."""

import asyncio
from contextlib import ExitStack
import os
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4, uuid5

from pydantic import ValidationError

from rag.contracts import ImageRef, SearchFragment, StepError
from rag.model_gateway.contracts import EmbeddingResult, RerankResult, RerankScore
from rag.runtime import _state, qdrant
from rag.runtime._config import RuntimeSettings
from rag.runtime.runner import _implemented
from rag.searcher import entrypoints as searcher


def fragment(revision, number):
    image = ImageRef(image_id=uuid5(revision, "image"), description="Схема")
    return SearchFragment(
        chunk_id=uuid5(revision, str(number)), document_id=uuid5(revision, "document"),
        revision_id=revision, source_name="Руководство", source_url=None,
        text=f"Раздел {number}. ![Схема](image://{image.image_id})", images=[image],
    )


class SearcherTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.dict(os.environ, {}, clear=True))
        self.settings = RuntimeSettings(
            _env_file=None, embedding_profile="embedding-test", embedding_dimensions=2,
            search_candidate_limit=10, search_top_k=2,
            embedding_timeout_s=7, rerank_timeout_s=9,
        )
        self.stack.enter_context(patch.object(
            searcher, "_get", return_value=SimpleNamespace(settings=self.settings)
        ))
        self.embedding = self.stack.enter_context(patch.object(
            searcher.models, "embed_texts", new=AsyncMock(return_value=EmbeddingResult(
                profile_id="embedding-test", vectors=[[0.1, 0.2]],
            )),
        ))
        self.revision = uuid4()
        self.revisions = frozenset({self.revision})
        self.fragments = [fragment(self.revision, index) for index in range(3)]
        self.hits = [qdrant.QdrantHit(fragment=item, score=score)
                     for item, score in zip(self.fragments, [0.8, 0.5, 0.2])]
        self.find = self.stack.enter_context(patch.object(
            searcher.qdrant, "find_chunks", new=AsyncMock(return_value=self.hits)
        ))
        self.rerank = self.stack.enter_context(patch.object(
            searcher.models, "rerank", new=AsyncMock(return_value=RerankResult(
                profile_id="reranker-test", scores=[
                    RerankScore(candidate_index=2, score=3),
                    RerankScore(candidate_index=0, score=-1),
                    RerankScore(candidate_index=1, score=3),
                ],
            )),
        ))

    async def test_embedding_search_limits_and_metadata(self):
        result = await searcher.search("Как войти?", self.revisions)
        self.assertEqual(result, self.fragments[:2])
        self.assertIs(result[0], self.fragments[0])
        self.assertEqual(result[0].images, self.fragments[0].images)
        self.embedding.assert_awaited_once_with(["Как войти?"], "query", timeout_s=7)
        self.find.assert_awaited_once_with(
            "Как войти?", [0.1, 0.2], self.revisions, 10, lexical_priority=False,
        )
        self.rerank.assert_not_awaited()
        self.assertTrue(_implemented(searcher.search))

    async def test_empty_revisions_do_not_require_runtime_or_models(self):
        with patch.object(searcher, "_get", side_effect=AssertionError("runtime used")):
            self.assertEqual(await searcher.search("Вопрос", frozenset()), [])
        self.embedding.assert_not_awaited()
        self.find.assert_not_awaited()
        self.rerank.assert_not_awaited()

    async def test_invalid_input_is_rejected_before_dependencies(self):
        for query, revisions in [
            (" ", self.revisions), (None, self.revisions),
            ("Вопрос", {self.revision}), ("Вопрос", frozenset({str(self.revision)})),
        ]:
            with self.subTest(query=query, revisions=revisions):
                with self.assertRaisesRegex(StepError, "search_invalid_input") as raised:
                    await searcher.search(query, revisions)
                self.assertFalse(raised.exception.retryable)
        self.embedding.assert_not_awaited()
        self.find.assert_not_awaited()

    async def test_missing_runtime_or_embedding_profile_is_explicit(self):
        with patch.object(searcher, "_get", side_effect=RuntimeError("not started")):
            with self.assertRaisesRegex(StepError, "search_not_ready"):
                await searcher.search("Вопрос", self.revisions)
        self.settings.embedding_profile = ""
        with self.assertRaisesRegex(StepError, "embedding_profile_not_configured"):
            await searcher.search("Вопрос", self.revisions)
        self.embedding.assert_not_awaited()

    async def test_incompatible_embedding_is_rejected_before_qdrant(self):
        for profile, vectors, code in [
            ("other", [[0.1, 0.2]], "embedding_profile_mismatch"),
            ("embedding-test", [], "model_invalid_response"),
            ("embedding-test", [[0.1, 0.2], [0.3, 0.4]], "model_invalid_response"),
            ("embedding-test", [[0.1]], "embedding_profile_mismatch"),
        ]:
            with self.subTest(profile=profile, vectors=vectors):
                self.embedding.return_value = EmbeddingResult(profile_id=profile, vectors=vectors)
                with self.assertRaisesRegex(StepError, code):
                    await searcher.search("Вопрос", self.revisions)
        self.find.assert_not_awaited()

    async def test_links_select_lexical_priority(self):
        for query in [
            "Открой https://wiki.local/guide", "HTTP://wiki/guide", "www.example.test",
            "Что в [инструкции](guide.md)?", "См. [раздел](#login)",
            "См. /wiki/login", "См. ../guide.md", "См. ./guide.md",
        ]:
            with self.subTest(query=query):
                await searcher.search(query, self.revisions)
                self.assertTrue(self.find.call_args.kwargs["lexical_priority"])
        for query in ["Ошибка AUTH-17", "Версия 1.2.3", "TCP/IP", "Включить и/или выключить"]:
            with self.subTest(query=query):
                await searcher.search(query, self.revisions)
                self.assertFalse(self.find.call_args.kwargs["lexical_priority"])

    async def test_rerank_uses_candidate_indices_and_stable_ties(self):
        self.settings.use_reranker = True
        result = await searcher.search("Вопрос", self.revisions)
        self.assertEqual(result, self.fragments[1:])
        self.rerank.assert_awaited_once_with(
            "Вопрос", [item.text for item in self.fragments], timeout_s=9,
        )
        self.assertEqual([hit.score for hit in self.hits], [0.8, 0.5, 0.2])

    async def test_empty_candidates_skip_rerank(self):
        self.settings.use_reranker = True
        self.find.return_value = []
        self.assertEqual(await searcher.search("Вопрос", self.revisions), [])
        self.rerank.assert_not_awaited()

    async def test_single_candidate_can_be_reranked(self):
        self.settings.use_reranker = True
        self.find.return_value = self.hits[:1]
        self.rerank.return_value = RerankResult(
            profile_id="reranker-test", scores=[RerankScore(candidate_index=0, score=1)],
        )
        self.assertEqual(await searcher.search("Вопрос", self.revisions), self.fragments[:1])

    async def test_threshold_uses_the_final_stage_score(self):
        self.settings.search_score_threshold = 0.5
        self.assertEqual(await searcher.search("Вопрос", self.revisions), self.fragments[:2])
        self.settings.use_reranker = True
        self.settings.search_score_threshold = 3
        self.assertEqual(await searcher.search("Вопрос", self.revisions), self.fragments[1:])
        self.settings.search_score_threshold = 4
        self.assertEqual(await searcher.search("Вопрос", self.revisions), [])

    async def test_invalid_rerank_mapping_is_not_silently_used(self):
        self.settings.use_reranker = True
        for indices in ([0, 1], [0, 0, 2], [0, 1, 3]):
            with self.subTest(indices=indices):
                self.rerank.return_value = RerankResult(
                    profile_id="reranker-test",
                    scores=[RerankScore(candidate_index=index, score=1) for index in indices],
                )
                with self.assertRaisesRegex(StepError, "model_invalid_response"):
                    await searcher.search("Вопрос", self.revisions)

    async def test_dependency_errors_keep_code_and_retryability_without_fallback(self):
        self.settings.use_reranker = True
        for operation, code, retryable in [
            (self.embedding, "model_timeout", True),
            (self.find, "qdrant_schema_error", False),
            (self.rerank, "model_unavailable", True),
        ]:
            with self.subTest(code=code):
                error = StepError(code, retryable)
                operation.side_effect = error
                with self.assertRaises(StepError) as raised:
                    await searcher.search("Вопрос", self.revisions)
                self.assertIs(raised.exception, error)
                operation.side_effect = None

    async def test_cancellation_propagates(self):
        self.embedding.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await searcher.search("Вопрос", self.revisions)
        self.find.assert_not_awaited()


class QdrantSearchTests(unittest.IsolatedAsyncioTestCase):
    @patch.dict(os.environ, {}, clear=True)
    async def test_search_uses_real_adapter_with_revision_filters_and_weighted_rrf(self):
        revision = uuid4()
        item = fragment(revision, 0)
        settings = RuntimeSettings(
            _env_file=None, embedding_profile="embedding-test", embedding_dimensions=2,
        )
        client = AsyncMock()
        client.query_points.return_value = SimpleNamespace(points=[SimpleNamespace(
            id=str(item.chunk_id), payload=item.model_dump(mode="json"), score=0.7,
        )])
        state = _state._Resources(settings)
        state.qdrant = client
        with patch.object(_state, "_current", state), patch.object(
            qdrant._qdrant, "_check", new=AsyncMock()
        ), patch.object(searcher.models, "embed_texts", new=AsyncMock(return_value=
            EmbeddingResult(profile_id="embedding-test", vectors=[[0.1, 0.2]])
        )):
            revisions = frozenset({revision})
            self.assertEqual(await searcher.search("См. https://wiki/guide", revisions), [item])
            request = client.query_points.call_args.kwargs
            self.assertEqual(request["query"].rrf.weights, [1, 2])
            self.assertEqual(request["prefetch"][0].query, [0.1, 0.2])
            self.assertEqual(request["prefetch"][1].query.text, "См. https://wiki/guide")
            for branch in request["prefetch"]:
                self.assertEqual(branch.filter.must[0].match.any, [str(revision)])
            # A point from another revision is a technical failure, never evidence.
            client.query_points.return_value.points[0].payload["revision_id"] = str(uuid4())
            with self.assertRaisesRegex(StepError, "qdrant_invalid_data"):
                await searcher.search("Вопрос", revisions)


class SearchConfigurationTests(unittest.TestCase):
    def test_limits_budgets_and_threshold_are_loaded_from_runtime_environment(self):
        with patch.dict(os.environ, {
            "RAG_SEARCH_CANDIDATE_LIMIT": "12", "RAG_SEARCH_TOP_K": "4",
            "RAG_SEARCH_SCORE_THRESHOLD": "-0.5", "RAG_EMBEDDING_TIMEOUT_S": "8",
            "RAG_RERANK_TIMEOUT_S": "11", "RAG_USE_RERANKER": "true",
        }, clear=True):
            settings = RuntimeSettings(_env_file=None)
        self.assertEqual((settings.search_candidate_limit, settings.search_top_k), (12, 4))
        self.assertEqual(settings.search_score_threshold, -0.5)
        self.assertEqual((settings.embedding_timeout_s, settings.rerank_timeout_s), (8, 11))
        self.assertTrue(settings.use_reranker)

    def test_invalid_limits_and_nonfinite_scores_are_rejected(self):
        for values in [
            {"search_candidate_limit": 0}, {"search_top_k": 0},
            {"search_candidate_limit": 2, "search_top_k": 3},
            {"embedding_timeout_s": 0}, {"rerank_timeout_s": float("inf")},
            {"search_score_threshold": float("nan")},
        ]:
            with self.subTest(values=values), self.assertRaises(ValidationError):
                RuntimeSettings(_env_file=None, **values)
