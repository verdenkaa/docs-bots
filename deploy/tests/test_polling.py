"""Polling confirmation boundary, recovery and safe diagnostics; no real Telegram."""

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from deploy import polling


class PollingTests(unittest.IsolatedAsyncioTestCase):
    async def test_polling_mode_never_registers_a_webhook(self):
        from deploy.__main__ import main
        with patch('sys.argv', ['deploy', 'webhook']), patch('deploy.config.validate'), \
                patch('rag._configuration.environment_values', return_value={'TELEGRAM_UPDATE_MODE': 'polling'}), \
                patch('deploy.webhook.register', side_effect=AssertionError('Unexpected registration')):
            self.assertEqual(main(), 0)

    async def asyncSetUp(self):
        self.calls = []
        self.settings = SimpleNamespace(telegram_url='https://telegram.test/bot123:private-token',
                                        webhook_secret='private-secret')
        self.output = StringIO()
        redirect = redirect_stdout(self.output)
        redirect.__enter__()
        self.addCleanup(redirect.__exit__, None, None, None)

    def client(self, handler):
        def wrapped(request):
            payload = json.loads(request.content)
            self.calls.append((request.url.host, request.url.path.rsplit('/', 1)[-1], payload))
            if request.url.host == 'core':
                self.assertEqual(request.headers['X-Telegram-Bot-Api-Secret-Token'], 'private-secret')
            else:
                self.assertNotIn('X-Telegram-Bot-Api-Secret-Token', request.headers)
            return handler(request, payload)
        return httpx.AsyncClient(transport=httpx.MockTransport(wrapped))

    async def test_prepare_preserves_pending_updates(self):
        async with self.client(lambda *_: httpx.Response(200, json={'ok': True, 'result': True})) as client:
            await polling._Poller(client, self.settings, lambda: None).prepare()
        self.assertEqual(self.calls[0][1:], ('deleteWebhook', {'drop_pending_updates': False}))

    async def test_partial_batch_failure_keeps_failed_update_unconfirmed(self):
        polls, deliveries, heartbeats = [], [], []
        def handler(request, value):
            if request.url.host != 'core':
                polls.append(value)
                return httpx.Response(200, json={'ok': True, 'result': [
                    {'update_id': 10, 'message': {'text': 'private-text'}}, {'update_id': 11}]})
            deliveries.append(value['update_id'])
            return httpx.Response(503 if len(deliveries) == 2 else 200)
        async with self.client(handler) as client:
            worker = polling._Poller(client, self.settings, lambda: heartbeats.append(True))
            with self.assertRaises(polling._Retry):
                await worker.step()
            self.assertEqual(worker.offset, 11)
            await worker.step()
            self.assertEqual(worker.offset, 12)
        self.assertNotIn('offset', polls[0])
        self.assertEqual(polls[1]['offset'], 11)
        self.assertEqual(deliveries, [10, 11, 11])
        self.assertTrue(heartbeats)
        self.assertNotIn('private-text', self.output.getvalue())

    async def test_uncertain_intake_response_replays_same_id_after_restart(self):
        saved = []
        def handler(request, value):
            if request.url.host != 'core':
                return httpx.Response(200, json={'ok': True, 'result': [{'update_id': 27}]})
            saved.append(value['update_id'])
            if len(saved) == 1:
                raise httpx.ReadTimeout('Response lost after durable intake', request=request)
            return httpx.Response(200)  # existing intake returns duplicate/200
        async with self.client(handler) as client:
            worker = polling._Poller(client, self.settings, lambda: None)
            with self.assertRaises(polling._Retry):
                await worker.step()
            self.assertIsNone(worker.offset)
            restarted = polling._Poller(client, self.settings, lambda: None)
            await restarted.step()
            self.assertEqual(restarted.offset, 28)
        self.assertEqual(saved, [27, 27])

    async def test_mini_app_navigation_is_sent_before_polling_ack(self):
        from fastapi import FastAPI
        from rag.rag_api.telegram.router import router
        from rag.rag_api import _mini_app_webhook as navigation

        app = FastAPI()
        app.include_router(router)
        update = {'update_id': 100, 'message': {
            'from': {'id': 7, 'is_bot': False},
            'chat': {'id': 7, 'type': 'private'}, 'text': '/start'}}
        polls = []
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url='http://core:8000') as intake:
            async def transport(request):
                if request.url.host == '127.0.0.1':
                    self.assertEqual(str(request.url), 'http://127.0.0.1:8000/telegram/webhook')
                    response = await intake.post('/telegram/webhook', content=request.content,
                                                 headers=request.headers)
                    self.assertEqual(response.content, b'')
                    return response
                value = json.loads(request.content)
                polls.append(value)
                return httpx.Response(200, json={'ok': True, 'result': (
                    [] if value.get('offset') == 101 else [update])})

            with patch.dict(os.environ, {
                'MINI_APP_ENABLED': 'true', 'MINI_APP_URL': 'https://mini.example.org',
                'TELEGRAM_BOT_TOKEN': '123:synthetic',
                'TELEGRAM_WEBHOOK_SECRET': 'private-secret',
                'TELEGRAM_WEBHOOK_URL': 'https://rag.example.org',
            }), patch.object(navigation, 'record_update', new=AsyncMock()) as record, \
                    patch.object(navigation.adapter, '_post', new=AsyncMock(side_effect=[
                        (503, {'ok': False, 'error_code': 503}),
                        (200, {'ok': True, 'result': {'message_id': 42}}),
                    ])) as send:
                async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
                    worker = polling._Poller(client, self.settings, lambda: None,
                                             intake_url='http://127.0.0.1:8000/telegram/webhook')
                    with self.assertRaises(polling._Retry):
                        await worker.step()
                    self.assertIsNone(worker.offset)
                    record.assert_not_awaited()
                    await worker.step()
                    self.assertEqual(worker.offset, 101)
                    record.assert_awaited_once()
                    self.assertEqual(send.await_count, 2)
                    self.assertEqual(send.call_args.args[0], 'sendMessage')
                    self.assertEqual(send.call_args.args[1]['reply_markup']['inline_keyboard'][0][0],
                                     {'text': 'Открыть приложение',
                                      'web_app': {'url': 'https://mini.example.org'}})
                    await worker.step()
        self.assertNotIn('offset', polls[0])
        self.assertNotIn('offset', polls[1])
        self.assertEqual(polls[2]['offset'], 101)

    async def test_invalid_batch_is_never_partially_confirmed(self):
        for updates in ([{'update_id': 10}, {'update_id': True}], [{'update_id': 10}, {'update_id': 10}]):
            async with self.client(lambda *_: httpx.Response(200, json={'ok': True, 'result': updates})) as client:
                worker = polling._Poller(client, self.settings, lambda: None)
                with self.assertRaises(polling._Retry):
                    await worker.step()
                self.assertIsNone(worker.offset)
        self.assertFalse(any(call[0] == 'core' for call in self.calls))

    async def test_rate_limit_honored_and_competing_receiver_fails(self):
        async with self.client(lambda *_: httpx.Response(429, json={'parameters': {'retry_after': 17}})) as client:
            with self.assertRaises(polling._Retry) as error:
                await polling._Poller(client, self.settings).step()
            self.assertEqual(error.exception.delay, 17)
        async with self.client(lambda *_: httpx.Response(409, text='private-token')) as client:
            with self.assertRaises(polling._Fatal) as error:
                await polling._Poller(client, self.settings).step()
            self.assertNotIn('private-token', str(error.exception))

    async def test_permanent_intake_rejection_does_not_ack(self):
        def handler(request, _):
            return (httpx.Response(403) if request.url.host == 'core' else
                    httpx.Response(200, json={'ok': True, 'result': [{'update_id': 10}]}))
        async with self.client(handler) as client:
            worker = polling._Poller(client, self.settings, lambda: None)
            with self.assertRaises(polling._Fatal):
                await worker.step()
            self.assertIsNone(worker.offset)


@unittest.skipIf(os.name == 'nt', 'Shared spool locking requires Linux')
class PollingLockTests(unittest.TestCase):
    def test_only_one_process_can_own_the_spool_lock(self):
        with TemporaryDirectory() as directory, patch.object(polling, '_LOCK', Path(directory)/'poll.lock'):
            with polling._exclusive():
                with self.assertRaises(polling._Fatal):
                    with polling._exclusive():
                        self.fail('Concurrent receiver admitted')
            with polling._exclusive():
                pass
