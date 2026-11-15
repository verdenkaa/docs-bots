"""Observability records actual Relay and recovery transitions without payloads."""
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
import json
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from rag.contracts import RequestRef, StorageError
from rag.runtime import queue_relay, rabbitmq, runner
from rag.runtime.postgresql.outbox import ReadyOutboxRow


class RelayTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def capture(self, *, publish_error=None, mark_error=None, marked=True):
        row = ReadyOutboxRow(
            notice=rabbitmq.QuestionNotice(message_id=uuid4(), route="rag.questions", event=RequestRef(request_id=7)),
            available_at=datetime.now(timezone.utc) - timedelta(seconds=5),
        )
        output = StringIO()
        with patch.object(queue_relay, "list_ready_outbox", AsyncMock(return_value=[row])), \
             patch.object(queue_relay, "publish_notice", AsyncMock(side_effect=publish_error)), \
             patch.object(queue_relay, "mark_outbox_published", AsyncMock(return_value=marked, side_effect=mark_error)), \
             redirect_stdout(output):
            if publish_error or mark_error:
                with self.assertRaises(type(publish_error or mark_error)):
                    await queue_relay.relay_outbox_batch(10)
            else:
                self.assertEqual(await queue_relay.relay_outbox_batch(10), int(marked))
        self.assertNotIn("private", output.getvalue())
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([r["event"] for r in records], ["started", "finished"])
        self.assertEqual(records[0]["run_id"], records[1]["run_id"])
        self.assertEqual(records[1]["request_id"], 7)
        self.assertGreaterEqual(records[1]["outbox_wait_ms"], 5000)
        return records[1]

    async def test_confirm_and_mark_are_distinct(self):
        record = await self.capture()
        self.assertEqual((record["outcome"], record["broker_confirmed"], record["outbox_marked"]),
                         ("published", True, True))
        record = await self.capture(marked=False)
        self.assertEqual((record["broker_confirmed"], record["outbox_marked"]), (True, False))

    async def test_broker_failure_and_unknown_database_commit_are_distinct(self):
        record = await self.capture(publish_error=rabbitmq.BrokerPublishError("private"))
        self.assertEqual((record["outcome"], record["broker_confirmed"], record["error_code"]),
                         ("failed", False, "BrokerPublishError"))
        record = await self.capture(mark_error=StorageError("private"))
        self.assertEqual((record["outcome"], record["broker_confirmed"], record["outbox_marked"]),
                         ("failed", True, False))


class RecoveryTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_recovery_count_reports_real_created_notifications(self):
        output = StringIO()
        with patch.object(runner.outbox, "recover_pending_work", AsyncMock(side_effect=[2, 0, 1])), \
             patch.object(runner, "_cleanup_batch", AsyncMock()), redirect_stdout(output):
            await runner._recover_batch(10)
        records = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual([(r["route"], r["recovered_count"]) for r in records],
                         [("rag.questions", 2), ("ingestion.jobs", 1)])
        self.assertTrue(all(r["operation"] == "recover_pending_work" for r in records))


class ConsumerTelemetryTests(unittest.IsolatedAsyncioTestCase):
    async def test_returned_retry_and_broker_redelivery_are_visible(self):
        import asyncio
        for decision in ("ack", "retry_broker"):
            with self.subTest(decision=decision):
                stop = asyncio.Event()
                message = SimpleNamespace(message_id=str(uuid4()), content_type="application/json",
                    body=b'{"request_id":7}', redelivered=True, ack=AsyncMock(), nack=AsyncMock())
                channel, queue, broker = AsyncMock(), AsyncMock(), AsyncMock()
                channel.declare_queue.return_value = queue
                broker.channel.return_value = channel
                state = SimpleNamespace(broker=broker, settings=SimpleNamespace(prefetch=1, queue_prefix="",
                    broker_retry_s=0, shutdown_timeout_s=1))
                async def receive(callback, **kwargs):
                    await callback(message)
                    stop.set()
                    return "tag"
                queue.consume.side_effect = receive
                output = StringIO()
                with patch.object(rabbitmq, "_get", return_value=state), \
                     patch.object(rabbitmq, "_connect_broker", AsyncMock()), redirect_stdout(output):
                    await rabbitmq._consume("rag.questions", AsyncMock(return_value=decision), stop)
                record = json.loads(output.getvalue())
                self.assertEqual((record["outcome"], record["consume_decision"], record["broker_redelivered"]),
                                 ("handled", decision, True))
                self.assertEqual(message.ack.await_count, int(decision == "ack"))
                self.assertEqual(message.nack.await_count, int(decision == "retry_broker"))
