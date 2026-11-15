"""Behavioral checks against docs/documents/faq/logging.md."""
import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timezone, timedelta
from io import StringIO
import json
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from test_rag_core import CoreTests as _CoreFixture, fragment
from test_model_gateway import ModelGatewayTests as _ModelFixture
from rag.contracts import (
    RagResult, StorageError, StepError, ImageQuestion, RejectedQuestion,
    OutOfScope, DetectorUnavailable, InsufficientEvidence, GeneratedAnswer,
)
from rag.rag_core import entrypoints as core, answer_generator
from rag.model_gateway.contracts import ModelGenerationResult
from rag.model_gateway.entrypoints import embed_texts
from rag.runtime import events, rabbitmq
from rag.rag_core import exact_cache
from rag.runtime.redis import CacheUnavailable, CacheRead, CachedAnswer


class CoreLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.fixture = _CoreFixture("test_cache_hit")
        self.fixture.setUp()
        self.addCleanup(self.fixture.stack.close)

    async def capture(self, exception=None):
        output = StringIO()
        received = "2026-11-15T00:00:00Z"
        with redirect_stdout(output), events._event_context(
            message_id="message", route="rag.questions", received_at=received
        ):
            if exception:
                with self.assertRaises(exception):
                    await self.fixture.run_question()
            else:
                await self.fixture.run_question()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        final = records[-1]
        self.assertEqual(records[0]["run_id"], final["run_id"])
        self.assertEqual(final["received_at"], received)
        self.assertLessEqual(datetime.fromisoformat(final["started_at"]),
                             datetime.fromisoformat(final["finished_at"]))
        self.assertEqual(final["started_at"], records[0]["started_at"])
        self.assertIsNone(events._trace.get())
        self.assertIsNone(events._parent_step.get())
        self.assertNotIn("How?", output.getvalue())
        self.assertNotIn("PRIVATE", output.getvalue())
        def check_steps(steps):
            offsets = [step["offset_ms"] for step in steps]
            self.assertEqual(offsets, sorted(offsets))
            for step in steps:
                self.assertGreaterEqual(step["duration_ms"], 0)
                self.assertGreaterEqual(step["offset_ms"], 0)
                check_steps(step.get("steps", []))
        check_steps(final["steps"])
        return final

    async def test_cache_hit_and_commit_are_explicit(self):
        self.fixture.get.return_value = RagResult(answer_text="PRIVATE", sources=[self.fixture.f])
        final = await self.capture()
        self.assertEqual(final["outcome"], "completed")
        self.assertEqual(final["consume_decision"], "ack")
        self.assertEqual(final["result_status"], "answered")
        self.assertEqual([s["outcome"] for s in final["steps"]],
                         ["claimed", "loaded", "allowed", "cache_hit", "committed"])

    async def test_empty_search_and_stale_commit(self):
        self.fixture.search.return_value = []
        self.fixture.complete.return_value = False
        final = await self.capture()
        self.assertEqual(final["outcome"], "stale_attempt")
        self.assertNotIn("result_status", final)
        steps = {s["name"]:s for s in final["steps"]}
        self.assertEqual(steps["get_answer"]["outcome"], "cache_miss")
        self.assertEqual(steps["search"]["outcome"], "no_fragments")
        self.assertEqual(steps["complete_question"]["outcome"], "stale_attempt")

    async def test_retry_and_lost_attempt(self):
        self.fixture.search.side_effect = StepError("temporary", True)
        for scheduled, expected in ((True, "retry_scheduled"), (False, "stale_attempt")):
            self.fixture.retry.return_value = scheduled
            final = await self.capture()
            self.assertEqual(final["outcome"], expected)
            self.assertEqual(final["steps"][-1]["outcome"], expected)
            self.assertEqual(final["error_code"], "temporary")
            self.assertEqual(final["consume_decision"], "ack")
            self.assertTrue(final["retryable"])
            self.assertTrue(final["steps"][-2]["retryable"])
            self.assertEqual(datetime.fromisoformat(final["steps"][-1]["available_at"]),
                             self.fixture.retry.call_args.args[1])

    async def test_unexpected_error_and_cancellation_do_not_claim_ack(self):
        for error, outcome, code in ((RuntimeError("PRIVATE"), "failed", "RuntimeError"),
                                     (asyncio.CancelledError(), "cancelled", "cancelled")):
            self.fixture.search.side_effect = error
            final = await self.capture(type(error))
            self.assertNotIn("consume_decision", final)
            self.assertEqual(final["outcome"], outcome)
            self.assertEqual(final["error_code"], code)
            self.assertEqual(final["steps"][-1]["outcome"], outcome)

    async def test_storage_failure_and_unclaimed_question(self):
        self.fixture.complete.side_effect = StorageError()
        final = await self.capture()
        self.assertEqual(final["consume_decision"], "retry_broker")
        self.assertEqual(final["outcome"], "failed")
        self.fixture.claim.return_value = None
        final = await self.capture()
        self.assertEqual(final["outcome"], "not_claimed")
        self.assertNotIn("attempt_no", final)

    async def test_business_reasons_are_recorded_only_after_commit(self):
        for kind in ("blocked", "invalid_input", "out_of_scope"):
            self.fixture.guard.return_value = (OutOfScope(kind=kind) if kind == "out_of_scope"
                                               else RejectedQuestion(kind=kind))
            for committed in (True, False):
                with self.subTest(kind=kind, committed=committed):
                    self.fixture.complete.return_value = committed
                    final = await self.capture()
                    step = final["steps"][-1]
                    self.assertEqual(step["outcome_reason"], kind)
                    if committed:
                        self.assertEqual(final["result_reason"], kind)
                    else:
                        self.assertNotIn("result_reason", final)
                        self.assertNotIn("result_status", final)
        self.fixture.get.assert_not_awaited()

    async def test_no_evidence_paths_have_distinct_steps(self):
        for branch in ("empty_corpus", "no_fragments", "insufficient_evidence"):
            with self.subTest(branch=branch):
                self.fixture.revisions.return_value = (frozenset() if branch == "empty_corpus"
                                                       else frozenset({self.fixture.f.revision_id}))
                self.fixture.search.return_value = [] if branch == "no_fragments" else [self.fixture.f]
                self.fixture.generate.return_value = InsufficientEvidence(kind="insufficient_evidence")
                final = await self.capture()
                self.assertEqual(final["result_reason"], "no_evidence")
                self.assertIn(branch, [step["outcome"] for step in final["steps"]])
                self.assertEqual(final["corpus_generation"], self.fixture.q.corpus_generation)

    async def test_ocr_reuse_and_empty_text_are_visible(self):
        self.fixture.q.input = ImageQuestion(kind="image", file_id="PRIVATE")
        final = await self.capture()
        names = [step["name"] for step in final["steps"]]
        self.assertEqual(names[1:4], ["get_question_image", "recognize_text", "save_prepared_question"])
        self.assertFalse(final["prepared_text_reused"])
        self.assertEqual(final["input_kind"], "image")
        self.fixture.q.prepared_text = "PRIVATE"
        final = await self.capture()
        self.assertTrue(final["prepared_text_reused"])
        self.assertNotIn("recognize_text", [step["name"] for step in final["steps"]])
        self.fixture.q.prepared_text = None
        self.fixture.ocr.return_value.text = ""
        final = await self.capture()
        self.assertEqual(final["result_reason"], "no_text")

    async def test_detector_unavailable_and_exhausted_retries(self):
        self.fixture.guard.return_value = DetectorUnavailable(kind="detector_unavailable", retryable=True)
        self.fixture.q.attempt.attempt_no = 3
        final = await self.capture()
        self.assertEqual(final["result_reason"], "technical_error")
        self.assertEqual(final["error_code"], "detector_unavailable")
        self.assertTrue(final["retryable"])
        step = next(step for step in final["steps"] if step["name"] == "check_question")
        self.assertTrue(step["retryable"])
        self.fixture.retry.assert_not_awaited()

    async def test_attempt_timeout_is_not_external_cancellation(self):
        async def slow_search(*args):
            await asyncio.sleep(60)
        self.fixture.search.side_effect = slow_search
        self.fixture.q.attempt.deadline_at = datetime.now(timezone.utc) + timedelta(seconds=0.1)
        final = await self.capture()
        step = next(step for step in final["steps"] if step["name"] == "search")
        self.assertEqual((step["outcome"], step["error_code"]), ("failed", "question_timeout"))
        self.assertEqual(final["outcome"], "retry_scheduled")

    async def test_invalid_citation_points_to_result_assembly(self):
        self.fixture.generate.return_value = GeneratedAnswer(
            kind="answered", answer_text="PRIVATE", cited_chunk_ids=[uuid4()])
        final = await self.capture()
        step = next(step for step in final["steps"] if step["name"] == "build_result")
        self.assertEqual(step["error_code"], "generation_invalid_response")
        self.assertEqual(final["result_reason"], "technical_error")

    async def test_generator_context_and_incomplete_response_are_visible(self):
        self.fixture.generate.side_effect = _generate_answer
        excluded = fragment()
        excluded.text = "PRIVATE" * 4000
        self.fixture.search.return_value = [self.fixture.f, excluded]
        response = ModelGenerationResult(profile_id="test", text="PRIVATE", finish_reason="length", usage=None)
        with patch.object(answer_generator.models, "generate", new=AsyncMock(return_value=response)):
            final = await self.capture()
        step = next(step for step in final["steps"] if step["name"] == "generate_answer")
        self.assertEqual(step["fragment_count"], 2)
        self.assertEqual(step["included_fragment_count"], 1)
        self.assertLessEqual(step["input_bytes"], step["max_input_bytes"])
        self.assertEqual(step["finish_reason"], "length")
        self.assertEqual(step["error_code"], "generation_incomplete")


class ModelLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = _ModelFixture("test_all_roles_and_empty_batches")
        await self.fixture.asyncSetUp()
        await self.fixture.start("embedding")

    async def asyncTearDown(self):
        try:
            await self.fixture.asyncTearDown()
        finally:
            self.fixture.redirect.__exit__(None, None, None)
            self.fixture.env.stop()
            self.fixture.temp.cleanup()

    async def test_concurrent_attempts_have_separate_nested_model_steps(self):
        async def search():
            await embed_texts(["PRIVATE"], "query", timeout_s=1)
            return []
        async def attempt(request_id):
            steps = []
            with events._trace_context(monotonic(), steps), events._event_context(request_id=request_id):
                await core._step("search", search())
            return steps
        first, second = await asyncio.gather(attempt(1), attempt(2))
        self.assertIsNot(first[0], second[0])
        for steps in (first, second):
            child = steps[0]["steps"][0]
            self.assertEqual(child["name"], "embed_texts")
            self.assertEqual(child["model_role"], "embedding")
            self.assertEqual(child["profile_id"], self.fixture.bindings["embedding"].profile_id)
            self.assertEqual(child["execution"], "remote")
            self.assertEqual(child["outcome"], "completed")
            for field in ("wait_ms", "inference_ms", "duration_ms", "offset_ms"):
                self.assertGreaterEqual(child[field], 0)
            self.assertGreaterEqual(child["offset_ms"], steps[0]["offset_ms"])
        self.assertNotIn("PRIVATE", self.fixture.output.getvalue())
        self.assertIsNone(events._trace.get())

    async def test_failed_model_is_a_failed_nested_step(self):
        steps = []
        with events._trace_context(monotonic(), steps):
            with self.assertRaises(StepError):
                await core._step("search", embed_texts(["PRIVATE"], "query", timeout_s=0))
        self.assertEqual(steps[0]["steps"][0]["error_code"], "model_invalid_input")
        self.assertEqual(steps[0]["outcome"], "failed")


class BrokerLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_receive_time_and_retry_decision_are_correlated(self):
        stop = asyncio.Event()
        message = SimpleNamespace(message_id=str(uuid4()), content_type="application/json",
                                  body=b'{"request_id":7}', ack=AsyncMock(), nack=AsyncMock())
        queue, channel, broker = AsyncMock(), AsyncMock(), AsyncMock()
        channel.declare_queue.return_value = queue
        broker.channel.return_value = channel
        settings = SimpleNamespace(prefetch=1, queue_prefix="", broker_retry_s=0,
                                   shutdown_timeout_s=1)
        state = SimpleNamespace(settings=settings, broker=broker)
        seen = {}
        async def handler(event):
            seen.update(events._context.get())
            raise RuntimeError("PRIVATE")
        async def consume(receive, **kwargs):
            await receive(message)
            stop.set()
            return "tag"
        queue.consume.side_effect = consume
        output = StringIO()
        with patch.object(rabbitmq, "_get", return_value=state), patch.object(
            rabbitmq, "_connect_broker", new=AsyncMock()
        ), redirect_stdout(output):
            await rabbitmq._consume("rag.questions", handler, stop)
        final = json.loads(output.getvalue())
        self.assertEqual(final["consume_decision"], "retry_broker")
        self.assertEqual(final["request_id"], 7)
        self.assertEqual(final["message_id"], message.message_id)
        self.assertEqual(final["received_at"], seen["received_at"])
        self.assertIsNotNone(datetime.fromisoformat(seen["received_at"]).tzinfo)
        message.ack.assert_not_awaited()
        message.nack.assert_awaited_once_with(requeue=True)
        self.assertNotIn("PRIVATE", output.getvalue())


class CacheLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_unavailable_cache_is_preserved_in_parent_step(self):
        steps = []
        with events._trace_context(monotonic(), steps), patch.object(
            exact_cache.redis, "read_cached_answer", new=AsyncMock(side_effect=CacheUnavailable())
        ), redirect_stdout(StringIO()):
            self.assertIsNone(await core._step("get_answer", exact_cache.get_answer("PRIVATE", 1)))
        self.assertEqual(steps[0]["outcome"], "unavailable")
        self.assertEqual(steps[0]["error_code"], "cache_unavailable")
        self.assertNotIn("cache_reason", steps[0])

    async def test_conditional_cache_operations_are_visible(self):
        source = fragment()
        value = CachedAnswer(answer_text="PRIVATE", sources=[source], corpus_generation=4)
        entry = CacheRead(value=value, compare_token=b"PRIVATE")
        read = AsyncMock(return_value=entry)
        with patch.object(exact_cache.redis, "read_cached_answer", new=read), patch.object(
            exact_cache.redis, "delete_cached_answer_if_unchanged", new=AsyncMock(return_value=False)
        ), patch.object(exact_cache.redis, "write_cached_answer_if_not_older", new=AsyncMock()) as write:
            for generation, reason in ((3, "newer_generation"), (4, "matching_generation"), (5, "older_generation")):
                steps = []
                with events._trace_context(monotonic(), steps):
                    await core._step("get_answer", exact_cache.get_answer("PRIVATE", generation))
                self.assertEqual(len(steps), 1)
                self.assertEqual(steps[0]["cache_reason"], reason)
                self.assertEqual(steps[0]["cached_generation"], 4)
                if generation == 5:
                    self.assertEqual(steps[0]["steps"][-1]["outcome"], "not_deleted")
            for stored, expected in ((False, "skipped_newer_generation"), (True, "stored")):
                write.return_value = stored
                steps = []
                with events._trace_context(monotonic(), steps):
                    await core._step("store_answer", exact_cache.store_answer(
                        "PRIVATE", 3, RagResult(answer_text="PRIVATE", sources=[source])))
                self.assertEqual(steps[0]["outcome"], expected)
                self.assertNotIn("PRIVATE", json.dumps(steps))

    async def test_cache_timeout_keeps_committed_answer(self):
        fixture = _CoreFixture("test_cache_hit")
        fixture.setUp()
        self.addCleanup(fixture.stack.close)
        fixture.store.side_effect = _store_answer
        async def slow_write(*args):
            await asyncio.sleep(60)
        output = StringIO()
        with patch.object(exact_cache.redis, "write_cached_answer_if_not_older", new=slow_write), patch.dict(
            "os.environ", {"RAG_CORE_CACHE_TIMEOUT_S": "0.01"}
        ), redirect_stdout(output):
            await fixture.run_question()
            await fixture.run_question()
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        failures = [r for r in records if r["component"] == "exact_cache"]
        self.assertEqual(len({r["run_id"] for r in failures}), 2)
        for final in (r for r in records if r["operation"] == "handle_rag_question" and r["event"] == "finished"):
            self.assertEqual(final["result_status"], "answered")
            self.assertEqual(final["outcome"], "completed")
            self.assertEqual(final["steps"][-1]["outcome"], "unavailable")
            self.assertEqual(final["steps"][-1]["error_code"], "cache_timeout")
            self.assertNotIn("error_code", final)


_store_answer = exact_cache.store_answer
_generate_answer = answer_generator.generate_answer

# Imported fixture TestCases must not be collected a second time by discovery.
def load_tests(loader, tests, pattern):
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls) for cls in
        (CoreLoggingTests, ModelLoggingTests, BrokerLoggingTests, CacheLoggingTests))
