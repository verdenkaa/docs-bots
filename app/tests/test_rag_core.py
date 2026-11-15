"""Core orchestration tests use owner adapters without external services."""
import asyncio
import json
import unittest
from contextlib import ExitStack, redirect_stdout
from datetime import datetime, timezone, timedelta
from io import StringIO
from unittest.mock import AsyncMock, patch
from uuid import uuid4
from rag.contracts import *
from rag.rag_core import entrypoints as core, answer_generator as generator, exact_cache as cache
from rag.model_gateway.contracts import ModelGenerationResult
from rag.runtime.redis import CacheRead, CachedAnswer, CacheUnavailable

def fragment():
    return SearchFragment(chunk_id=uuid4(), document_id=uuid4(), revision_id=uuid4(),
                          source_name="Manual", text="Install the package.")

class CoreTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.f = fragment()
        self.q = ClaimedQuestion(attempt=ProcessingAttempt(request_id=1, attempt_no=1,
            deadline_at=datetime.now(timezone.utc)+timedelta(seconds=120)),
            input=TextQuestion(kind="text", text="How?"), received_at=datetime.now(timezone.utc),
            corpus_generation=4)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        def mock(owner, name, value):
            return self.stack.enter_context(patch.object(owner, name, new=AsyncMock(return_value=value)))
        self.claim = mock(core.storage, "claim_question", self.q)
        self.complete = mock(core.storage, "complete_question", True)
        self.retry = mock(core.storage, "schedule_question_retry", True)
        self.prepared = mock(core.storage, "save_prepared_question", True)
        self.revisions = mock(core.storage, "load_revision_ids", frozenset({self.f.revision_id}))
        self.guard = mock(core.guardrails, "check_question", AllowedQuestion(kind="allowed", normalized_question="How?"))
        self.get = mock(core.exact_cache, "get_answer", None)
        self.store = mock(core.exact_cache, "store_answer", None)
        self.search = mock(core.searcher, "search", [self.f])
        self.generate = mock(core.answer_generator, "generate_answer",
            GeneratedAnswer(kind="answered", answer_text="Install.", cited_chunk_ids=[self.f.chunk_id]))
        self.image = mock(core.media, "get_question_image", ImageContent(data=b"x", mime_type="image/png"))
        self.ocr = mock(core.models, "recognize_text", type("Ocr", (), {"text": "recognized"})())

    async def run_question(self):
        return await core.handle_rag_question(RequestRef(request_id=1))

    async def test_answer_commit_before_cache(self):
        self.store.side_effect = lambda *args: self.complete.assert_awaited_once()
        self.assertEqual(await self.run_question(), "ack")
        result = self.complete.call_args.args[1].result
        self.assertEqual(result.sources[0].chunk_id, self.f.chunk_id)
        self.search.assert_awaited_once_with("How?", frozenset({self.f.revision_id}))
        self.store.assert_awaited_once()

    async def test_stale_commit_does_not_cache(self):
        self.complete.return_value = False
        await self.run_question()
        self.store.assert_not_awaited()

    async def test_unknown_commit_retries_broker(self):
        self.complete.side_effect = StorageError()
        self.assertEqual(await self.run_question(), "retry_broker")
        self.store.assert_not_awaited()

    async def test_retry_and_exhaustion(self):
        self.search.side_effect = StepError("temporary", True)
        await self.run_question()
        self.retry.assert_awaited_once()
        self.complete.assert_not_awaited()
        self.q.attempt.attempt_no = 3
        await self.run_question()
        self.assertEqual(self.complete.call_args.args[1].code, "technical_error")

    async def test_over_limit_attempt_only_completes_technical_outcome(self):
        self.q.attempt.attempt_no = 3
        self.q.input = ImageQuestion(kind="image", file_id="file")
        with patch.dict("os.environ", {"RAG_CORE_MAX_ATTEMPTS": "2"}):
            for committed, expected in ((True, "ack"), (False, "ack"), (StorageError(), "retry_broker")):
                with self.subTest(committed=committed):
                    self.complete.reset_mock()
                    self.complete.side_effect = committed if isinstance(committed, Exception) else None
                    self.complete.return_value = committed
                    self.assertEqual(await self.run_question(), expected)
                    self.complete.assert_awaited_once()
                    attempt, outcome = self.complete.call_args.args
                    self.assertEqual(attempt, self.q.attempt)
                    self.assertEqual((outcome.kind, outcome.code), ("notice", "technical_error"))
        for operation in (self.image, self.ocr, self.prepared, self.revisions,
                          self.guard, self.get, self.search, self.generate, self.store, self.retry):
            operation.assert_not_awaited()

    async def test_last_allowed_attempt_still_runs_pipeline(self):
        self.q.attempt.attempt_no = 2
        with patch.dict("os.environ", {"RAG_CORE_MAX_ATTEMPTS": "2"}):
            self.assertEqual(await self.run_question(), "ack")
        self.generate.assert_awaited_once()
        self.assertEqual(self.complete.call_args.args[1].kind, "answered")

    async def test_attempt_logging_is_isolated_and_reaches_model_tasks(self):
        second = self.q.model_copy(deep=True)
        second.attempt.request_id, second.attempt.attempt_no = 2, 2
        questions = {1: self.q, 2: second}
        self.claim.side_effect = lambda request_id: questions[request_id]

        async def model_task():
            await asyncio.sleep(0)
            core.emit_event("model_gateway", "generate", "finished", str(uuid4()))
            return self.generate.return_value

        async def generate(*args):
            return await asyncio.create_task(model_task())

        self.generate.side_effect = generate

        async def handle(request_id):
            with core._event_context(message_id=f"message-{request_id}", route="rag.questions"):
                parent = core._context.get()
                decision = await core.handle_rag_question(RequestRef(request_id=request_id))
                self.assertIs(core._context.get(), parent)
                return decision

        parent = core._context.get()
        output = StringIO()
        with redirect_stdout(output):
            self.assertEqual(await asyncio.gather(handle(1), handle(2)), ["ack", "ack"])
        self.assertIs(core._context.get(), parent)
        events = [json.loads(line) for line in output.getvalue().splitlines()]
        for request_id, question in questions.items():
            own = [event for event in events if event["request_id"] == request_id]
            self.assertEqual(len([event for event in own if event["operation"] != "step"]), 3)
            started = next(event for event in own if event["event"] == "started")
            self.assertNotIn("attempt_no", started)
            finished = next(event for event in own if event["operation"] == "handle_rag_question" and event["event"] == "finished")
            self.assertEqual(started["run_id"], finished["run_id"])
            for event in own:
                self.assertEqual(event["message_id"], f"message-{request_id}")
                self.assertEqual(event["route"], "rag.questions")
                if event["event"] == "finished":
                    self.assertEqual(event["attempt_no"], question.attempt.attempt_no)

    async def test_attempt_logging_survives_retry_and_storage_failure(self):
        self.q.attempt.attempt_no = 2
        for storage_failure, expected in ((False, "ack"), (True, "retry_broker")):
            with self.subTest(storage_failure=storage_failure):
                self.search.side_effect = StepError("temporary", True)
                self.retry.side_effect = StorageError() if storage_failure else None
                output = StringIO()
                with core._event_context(message_id="message-1"):
                    parent = core._context.get()
                    with redirect_stdout(output):
                        self.assertEqual(await self.run_question(), expected)
                    self.assertIs(core._context.get(), parent)
                events = [json.loads(line) for line in output.getvalue().splitlines()]
                self.assertTrue(all(event["attempt_no"] == 2 for event in events[1:]))
                self.assertEqual(events[-1]["consume_decision"], expected)

    async def test_rejection_precedes_cache(self):
        for kind in ("blocked", "invalid_input", "out_of_scope"):
            self.guard.return_value = (OutOfScope(kind=kind) if kind == "out_of_scope" else RejectedQuestion(kind=kind))
            await self.run_question()
        self.get.assert_not_awaited()
        self.search.assert_not_awaited()

    async def test_empty_corpus(self):
        self.revisions.return_value = frozenset()
        await self.run_question()
        self.assertEqual(self.complete.call_args.args[1].reason, "no_evidence")
        self.guard.assert_awaited_once()
        self.get.assert_not_awaited()

    async def test_cache_hit(self):
        self.get.return_value = RagResult(answer_text="cached", sources=[SourceRef.model_validate(
            self.f.model_dump(exclude={"text", "images"}))])
        await self.run_question()
        self.search.assert_not_awaited()
        self.store.assert_not_awaited()

    async def test_no_evidence(self):
        self.search.return_value = []
        await self.run_question()
        self.assertEqual(self.complete.call_args.args[1].reason, "no_evidence")
        self.generate.assert_not_awaited()

    async def test_unknown_citation(self):
        self.generate.return_value = GeneratedAnswer(kind="answered", answer_text="bad", cited_chunk_ids=[uuid4()])
        await self.run_question()
        self.assertEqual(self.complete.call_args.args[1].code, "technical_error")

    async def test_ocr_saved_and_reused(self):
        self.q.input = ImageQuestion(kind="image", file_id="file", caption="caption")
        await self.run_question()
        self.prepared.assert_awaited_once_with(self.q.attempt, "caption\n\nrecognized")
        self.q.prepared_text = "saved"
        self.ocr.reset_mock()
        await self.run_question()
        self.ocr.assert_not_awaited()
        self.guard.assert_awaited_with("saved", frozenset({self.f.revision_id}))

    async def test_unclaimed_question(self):
        self.claim.return_value = None
        self.assertEqual(await self.run_question(), "ack")
        self.guard.assert_not_awaited()
        self.complete.assert_not_awaited()

    async def test_lost_attempt_after_ocr(self):
        self.q.input = ImageQuestion(kind="image", file_id="file")
        self.prepared.return_value = False
        await self.run_question()
        self.guard.assert_not_awaited()
        self.complete.assert_not_awaited()

    async def test_insufficient_evidence(self):
        self.generate.return_value = InsufficientEvidence(kind="insufficient_evidence")
        await self.run_question()
        self.assertEqual(self.complete.call_args.args[1].reason, "no_evidence")
        self.store.assert_not_awaited()

    async def test_empty_ocr(self):
        self.q.input = ImageQuestion(kind="image", file_id="file")
        self.ocr.return_value.text = ""
        await self.run_question()
        self.assertEqual(self.complete.call_args.args[1].code, "no_text")
        self.guard.assert_not_awaited()

class GeneratorTests(unittest.IsolatedAsyncioTestCase):
    async def test_model_formats(self):
        f = fragment()
        for data, valid in [
            ({"kind":"answered","answer_text":"Install","citations":["S1"]}, True),
            ({"kind":"insufficient_evidence"}, True),
            ({"kind":"answered","answer_text":"Install","citations":["S2"]}, False),
            ({"kind":"answered","answer_text":"","citations":["S1"]}, False),
            ({"kind":"answered","answer_text":"Install","citations":["S1"],"cited_image_ids":[str(uuid4())]}, False),
        ]:
            response = ModelGenerationResult(profile_id="test", text=json.dumps(data), finish_reason="completed", usage=None)
            with patch.object(generator.models, "generate", AsyncMock(return_value=response)):
                if valid:
                    await generator.generate_answer("How?", [f])
                else:
                    with self.assertRaises(StepError):
                        await generator.generate_answer("How?", [f])

    async def test_empty_budget_and_incomplete_generation(self):
        with patch.dict("os.environ", {"RAG_CORE_MAX_INPUT_BYTES":"1"}):
            with self.assertRaises(StepError):
                await generator.generate_answer("How?", [fragment()])
        response = ModelGenerationResult(profile_id="test", text="", finish_reason="length", usage=None)
        with patch.object(generator.models, "generate", AsyncMock(return_value=response)):
            with self.assertRaises(StepError) as error:
                await generator.generate_answer("How?", [fragment()])
            self.assertEqual(error.exception.code, "generation_incomplete")

class CacheTests(unittest.IsolatedAsyncioTestCase):
    async def test_generations(self):
        f = fragment()
        value = CachedAnswer(answer_text="Answer", sources=[SourceRef.model_validate(
            f.model_dump(exclude={"text","images"}))], corpus_generation=4)
        entry = CacheRead(value=value, compare_token=b"token")
        with patch.object(cache.redis, "read_cached_answer", AsyncMock(return_value=entry)), patch.object(
            cache.redis, "delete_cached_answer_if_unchanged", AsyncMock()) as delete:
            self.assertIsNotNone(await cache.get_answer("How?", 4))
            self.assertIsNone(await cache.get_answer("How?", 3))
            delete.assert_not_awaited()
            self.assertIsNone(await cache.get_answer("How?", 5))
            delete.assert_awaited_once_with("How?", b"token")

    async def test_unavailable(self):
        with patch.object(cache.redis, "read_cached_answer", AsyncMock(side_effect=CacheUnavailable())):
            self.assertIsNone(await cache.get_answer("How?", 4))

class ImageCitationTests(unittest.IsolatedAsyncioTestCase):
    async def test_images_require_cited_included_chunk(self):
        image = ImageRef(image_id=uuid4(), description="Diagram")
        f = fragment()
        f.text = "See ![Diagram](image://" + str(image.image_id) + ")"
        f.images = [image]
        data = {"kind":"answered", "answer_text":f.text, "citations":["S1"],
                "cited_image_ids":[str(image.image_id)]}
        response = ModelGenerationResult(profile_id="test", text=json.dumps(data),
                                          finish_reason="completed", usage=None)
        with patch.object(generator.models, "generate", AsyncMock(return_value=response)):
            generated = await generator.generate_answer("How?", [f])
            result = core._result(generated, [f])
            self.assertEqual(result.images, [image])
        other = fragment()
        data["citations"] = ["S2"]
        response.text = json.dumps(data)
        with patch.object(generator.models, "generate", AsyncMock(return_value=response)):
            with self.assertRaises(StepError):
                await generator.generate_answer("How?", [f, other])

    async def test_cache_timeout_is_miss(self):
        import asyncio
        async def slow(query):
            await asyncio.sleep(10)
        with patch.object(cache.redis, "read_cached_answer", slow), patch.dict(
                "os.environ", {"RAG_CORE_CACHE_TIMEOUT_S":"0.01"}):
            self.assertIsNone(await cache.get_answer("How?", 1))
