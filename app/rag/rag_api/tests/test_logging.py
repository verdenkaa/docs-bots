"""Behavioral checks for FAQ logging and transient startup recovery."""
import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import asyncpg
import httpx

from rag.contracts import DeliveryAttempt, DeliveryRef, RagResult, StorageError
from rag.rag_api import application, entrypoints, intake, support
from rag.rag_api.contracts import ClaimedDelivery, IntakeResult, Sent, SupportCase, TextPart
from rag.rag_api._observability import _operation
from rag.rag_api.delivery import sender
from rag.rag_api.telegram import adapter
from rag.runtime.events import _event_context


def _records(output):
    return [json.loads(line) for line in output.getvalue().splitlines()]


class LoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(patch.dict('os.environ', TELEGRAM_BOT_TOKEN='123:secret',
            TELEGRAM_WEBHOOK_SECRET='webhook-secret', TELEGRAM_WEBHOOK_URL='https://bot.example'))

    def check_steps(self, record):
        self.assertIn('started_at', record)
        self.assertIn('finished_at', record)
        self.assertGreaterEqual(record['duration_ms'], 0)
        offsets = []
        for step in record['steps']:
            self.assertIn('name', step)
            self.assertIn('outcome', step)
            self.assertGreaterEqual(step['duration_ms'], 0)
            offsets.append(step['offset_ms'])
        self.assertEqual(offsets, sorted(offsets))

    async def test_http_intake_has_one_final_record_and_real_registration_step(self):
        update = {'update_id': 123, 'message': {'from': {'id': 7, 'username': 'Employee'},
            'chat': {'id': 99, 'type': 'private'}, 'text': 'private-question'}}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url='http://test') as client:
            with patch.object(intake.repository, 'register_question', AsyncMock(return_value=IntakeResult(kind='accepted', request_id=10))):
                response = await client.post('/telegram/webhook', json=update,
                    headers={'X-Telegram-Bot-Api-Secret-Token': 'webhook-secret'})
        self.assertEqual(response.status_code, 200)
        records = _records(self.output)
        self.assertEqual(len(records), 1)
        record = records[0]
        self.check_steps(record)
        self.assertEqual((record['operation'], record['outcome'], record['request_id']), ('handle_telegram_update', 'accepted', 10))
        self.assertEqual(record['http_status'], 200)
        self.assertEqual(record['route'], '/telegram/webhook')
        self.assertEqual([s['name'] for s in record['steps']], ['validate_webhook_secret', 'validate_content_type',
            'decode_update', 'validate_update_id', 'parse_update', 'register_question'])
        for secret in ('private-question', '123:secret', 'webhook-secret'):
            self.assertNotIn(secret, self.output.getvalue())

    async def test_callback_ack_failure_is_a_step_in_the_same_event(self):
        update = {'update_id': 5, 'callback_query': {'id': 'private-callback',
            'from': {'id': 7, 'username': 'Employee'}, 'data': 's:10',
            'message': {'message_id': 1, 'chat': {'id': 99, 'type': 'private'}}}}
        with patch.object(support.repository, 'record_support_action', AsyncMock(return_value=IntakeResult(kind='handled'))), \
             patch.object(intake, '_answer_callback', AsyncMock(side_effect=RuntimeError('private-error'))):
            result = await entrypoints.handle_telegram_update(update)
        self.assertEqual(result.kind, 'handled')
        records = _records(self.output)
        self.assertEqual(len(records), 1)
        self.check_steps(records[0])
        steps = {s['name']: s for s in records[0]['steps']}
        self.assertEqual(steps['record_support_action']['action'], 'start')
        self.assertEqual(steps['answer_callback']['error_code'], 'callback_ack_failed')
        self.assertNotIn('private-error', self.output.getvalue())
        self.assertNotIn('private-callback', self.output.getvalue())

    async def test_storage_error_step_and_final_retry_are_not_success(self):
        with patch.object(intake.repository, 'record_update', AsyncMock(side_effect=StorageError('secret-dsn'))):
            result = await entrypoints.handle_telegram_update({'update_id': 6})
        self.assertEqual(result.kind, 'retry_later')
        record = _records(self.output)[0]
        self.assertEqual(record['error_code'], 'intake_storage_error')
        self.assertEqual(record['steps'][-1]['outcome'], 'error')
        self.assertNotIn('secret-dsn', self.output.getvalue())

    async def test_delivery_logs_claim_plan_receipt_and_complete(self):
        attempt = DeliveryAttempt(delivery_id=20, attempt_no=1, deadline_at=datetime.now(timezone.utc) + timedelta(seconds=60))
        delivery = ClaimedDelivery(attempt=attempt, user_id=7, chat_id=99, purpose='notice', request_id=10,
            content=RagResult(answer_text='private-answer', sources=[]))
        with patch.object(entrypoints.repository, 'claim_delivery', AsyncMock(return_value=delivery)), \
             patch.object(entrypoints.repository, 'store_delivery_parts', AsyncMock(return_value=True)), \
             patch.object(entrypoints.repository, 'record_delivery_part', AsyncMock(return_value=True)), \
             patch.object(entrypoints.repository, 'complete_delivery', AsyncMock(return_value=True)), \
             patch.object(entrypoints, 'send_answer', AsyncMock(return_value=Sent(kind='sent', message_id=77))):
            self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'ack')
        records = _records(self.output)
        self.assertEqual([r['event'] for r in records], ['started', 'finished'])
        self.assertEqual(records[0]['run_id'], records[1]['run_id'])
        final = records[1]
        self.check_steps(final)
        self.assertEqual(final['outcome'], 'delivered')
        self.assertEqual([s['name'] for s in final['steps']], ['claim_delivery', 'build_delivery_parts',
            'store_delivery_parts', 'send_part', 'record_delivery_part', 'complete_delivery'])
        receipt = final['steps'][-2]
        self.assertEqual((receipt['message_id'], receipt['part_no'], receipt['variant']), (77, 0, 'normal'))
        self.assertNotIn('private-answer', self.output.getvalue())

    async def test_actual_bot_attempts_log_http_status_and_fallback_variant(self):
        real_client = httpx.AsyncClient
        count = 0
        def respond(request):
            nonlocal count
            count += 1
            if count == 1:
                return httpx.Response(400, json={'ok': False, 'error_code': 400, 'description': 'message is too long'})
            return httpx.Response(200, json={'ok': True, 'result': {'message_id': 77}})
        def client_factory(**kwargs):
            return real_client(transport=httpx.MockTransport(respond), **kwargs)
        with patch.object(adapter.httpx, 'AsyncClient', side_effect=client_factory), \
             patch.object(adapter.limiter, 'wait', AsyncMock()):
            with _operation('test_delivery'):
                result = await sender._send(99, TextPart(kind='text', text='private-answer'))
        self.assertEqual(result.variant, 'length_fallback')
        record = _records(self.output)[0]
        self.check_steps(record)
        steps = [s for s in record['steps'] if s['name'] == 'sendMessage']
        self.assertEqual([s['http_status'] for s in steps], [400, 200])
        self.assertEqual([s['delivery_variant'] for s in steps], ['answer', 'length_fallback'])
        self.assertEqual(steps[1]['message_id'], 77)
        self.assertNotIn('123:secret', self.output.getvalue())
        self.assertNotIn('private-answer', self.output.getvalue())

    async def test_parallel_updates_keep_independent_steps_and_ids(self):
        async def register(key, *args):
            await asyncio.sleep(0)
            return IntakeResult(kind='ignored')
        with patch.object(intake.repository, 'record_update', side_effect=register):
            await asyncio.gather(*(entrypoints.handle_telegram_update({'update_id': key}) for key in (10, 11)))
        records = _records(self.output)
        self.assertEqual({r['update_id'] for r in records}, {10, 11})
        self.assertEqual(len({r['run_id'] for r in records}), 2)
        for record in records:
            self.assertEqual([s['name'] for s in record['steps']], ['parse_update', 'record_update'])

    async def test_broker_message_context_is_preserved(self):
        with _event_context(message_id='amqp-message', route='rag.results'):
            with patch.object(entrypoints.repository, 'claim_delivery', AsyncMock(return_value=None)):
                await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20))
        records = _records(self.output)
        for record in records:
            self.assertEqual(record['message_id'], 'amqp-message')
            self.assertEqual(record['route'], 'rag.results')

    async def test_due_confirmation_and_ticket_creation_have_case_steps(self):
        waiting = SupportCase(case_id=uuid4(), request_id=10, user_id=7, state='waiting',
            reason='human_request', check_at=datetime.now(timezone.utc) - timedelta(minutes=1))
        creating = SupportCase(case_id=uuid4(), request_id=11, user_id=7, state='creating', reason='no_evidence')
        opened = SupportCase(case_id=creating.case_id, request_id=11, user_id=7, state='open',
            reason='no_evidence', created_at=datetime.now(timezone.utc), ticket_url='https://private.example/ticket')
        with patch.object(support.repository, 'list_pending_support', AsyncMock(return_value=[waiting, creating])), \
             patch.object(support.repository, 'queue_support_confirmation', AsyncMock(return_value=True)), \
             patch.object(support.repository, 'create_support_ticket', AsyncMock(return_value=opened)):
            self.assertEqual(await support.run_support_batch(10), 2)
        records = _records(self.output)
        self.assertEqual(len(records), 1)
        self.check_steps(records[0])
        due, create = records[0]['steps'][1:]
        self.assertEqual((due['case_id'], due['outcome']), (str(waiting.case_id), 'recorded'))
        self.assertEqual((create['case_id'], create['outcome']), (str(creating.case_id), 'open'))
        self.assertNotIn('private.example', self.output.getvalue())

    async def test_delivery_storage_failure_is_visible_in_finished_event(self):
        with patch.object(entrypoints.repository, 'claim_delivery', AsyncMock(side_effect=StorageError('private-query'))):
            self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'retry_broker')
        record = _records(self.output)[-1]
        self.assertEqual(record['outcome'], 'storage_unknown')
        self.assertEqual(record['consume_decision'], 'retry_broker')
        self.assertEqual(record['steps'][0]['error_code'], 'storage_error')
        self.assertNotIn('private-query', self.output.getvalue())


class StartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))

    async def test_attached_connection_reset_is_retried_then_starts(self):
        with patch.object(application, 'start_runtime', AsyncMock(side_effect=[
                asyncpg.ConnectionDoesNotExistError('private-dsn'), None])) as start, \
             patch.object(application.asyncio, 'sleep', AsyncMock()) as sleep:
            await application._start_with_retry()
        self.assertEqual(start.await_count, 2)
        sleep.assert_awaited_once_with(1)
        self.assertEqual(_records(self.output)[-1]['outcome'], 'ready')
        self.assertNotIn('private-dsn', self.output.getvalue())

    async def test_persistent_connection_reset_has_bounded_retries(self):
        with patch.object(application, 'start_runtime', AsyncMock(side_effect=ConnectionResetError(64, 'private-dsn'))) as start, \
             patch.object(application.asyncio, 'sleep', AsyncMock()) as sleep:
            with self.assertRaisesRegex(RuntimeError, 'after 3 attempts'):
                await application._start_with_retry()
        self.assertEqual(start.await_count, 3)
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual(_records(self.output)[-1]['error_code'], 'postgresql_connection_lost')

    async def test_bad_password_and_missing_schema_are_not_retried(self):
        for error in (asyncpg.InvalidPasswordError('private-password'), RuntimeError('schema requires migration')):
            with patch.object(application, 'start_runtime', AsyncMock(side_effect=error)) as start, \
                 patch.object(application.asyncio, 'sleep', AsyncMock()) as sleep:
                with self.assertRaises(type(error)):
                    await application._start_with_retry()
            start.assert_awaited_once()
            sleep.assert_not_awaited()

    async def test_startup_deadline_cancels_hung_connection(self):
        settings = type('Settings', (), {'startup_timeout_s': 0.02})()
        async def hung():
            await asyncio.sleep(5)
        with patch.object(application, 'RuntimeSettings', return_value=settings), \
             patch.object(application, 'start_runtime', side_effect=hung):
            with self.assertRaisesRegex(RuntimeError, 'startup timed out'):
                await application._start_with_retry()
        self.assertEqual(_records(self.output)[-1]['error_code'], 'runtime_start_timeout')
