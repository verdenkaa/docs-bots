"""Behavioral checks against docs/documents/faq/logging.md."""
import asyncio
from contextlib import redirect_stdout
from datetime import datetime
from io import StringIO
import json
from time import monotonic
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from test_rag_core import CoreTests as _CoreFixture
from test_model_gateway import ModelGatewayTests as _ModelFixture
from rag.contracts import RagResult, StorageError, StepError
from rag.rag_core import entrypoints as core
from rag.model_gateway.entrypoints import embed_texts
from rag.runtime import events, rabbitmq
from rag.rag_core import exact_cache
from rag.runtime.redis import CacheUnavailable


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
        async def attempt(request_id):
            steps = []
            with events._trace_context(monotonic(), steps), events._event_context(request_id=request_id):
                await core._step("search", embed_texts(["PRIVATE"], "query", timeout_s=1))
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

# Imported fixture TestCases must not be collected a second time by discovery.
def load_tests(loader, tests, pattern):
    return unittest.TestSuite(loader.loadTestsFromTestCase(cls) for cls in
        (CoreLoggingTests, ModelLoggingTests, BrokerLoggingTests, CacheLoggingTests))
