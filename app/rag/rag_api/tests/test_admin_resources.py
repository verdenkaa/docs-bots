"""Authentication, resource budgets, pagination and actual delivery contracts."""

import asyncio
import base64
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from rag.contracts import StorageError
from rag.rag_api import access, application, support
from rag.rag_api.contracts import (
    AccessRecord, MetricsSnapshot, Principal, RateLimited, SupportCase, TicketMessage, TicketView, TextPart,
)
from rag.rag_api.delivery import sender
from rag.rag_api.delivery._texts import FALLBACK_TEXT
from rag.rag_api.health import router as health


class AdminResourceTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch.dict('os.environ', {
            'RAG_ENV_FILE': '__absent_test_env__', 'ADMIN_LOGIN': 'admin', 'ADMIN_PASSWORD': 'test-password',
            'TELEGRAM_BOT_TOKEN': '123:test', 'TELEGRAM_WEBHOOK_SECRET': 'test-secret',
            'TELEGRAM_WEBHOOK_URL': 'https://bot.example',
        }, clear=True))
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url='https://bot.example')
        self.addAsyncCleanup(self.client.aclose)
        self.auth = {'Authorization': 'Basic ' + base64.b64encode(b'admin:test-password').decode(),
                     'Origin': 'https://bot.example'}
        self.case_id, self.client_id = uuid4(), uuid4()
        self.body = {'client_message_id': str(self.client_id), 'text': 'Help'}

    async def test_all_admin_routes_require_auth_before_reading_body(self):
        for method, url in [('GET','/admin'), ('GET','/tickets'), ('GET','/admin/metrics'),
                            ('PUT','/admin/users/employee'), ('POST',f'/tickets/{self.case_id}/close'),
                            ('POST',f'/admin/tickets/{self.case_id}/messages')]:
            with self.subTest(url=url):
                read = False
                async def body():
                    nonlocal read
                    read = True
                    yield b'x' * 100000
                response = await self.client.request(method, url, content=body())
                self.assertEqual(response.status_code, 401)
                self.assertIn('Basic', response.headers['www-authenticate'])
                self.assertFalse(read)
        for value in ('Basic bad!', 'Bearer ignored', 'Basic ' + base64.b64encode(b'admin:wrong').decode()):
            self.assertEqual((await self.client.get('/tickets', headers={'Authorization': value})).status_code, 401)

    async def test_admin_identity_origin_and_normalized_access(self):
        record = AccessRecord(username='employee', enabled=False)
        with patch.object(access.repository, 'write_user_access', AsyncMock(return_value=record)) as write:
            for origin in (None, 'https://evil.example', 'null', 'https://bot.example.evil'):
                headers = dict(self.auth)
                if origin is None:
                    headers.pop('Origin')
                else:
                    headers['Origin'] = origin
                self.assertEqual((await self.client.put('/admin/users/Employee', headers=headers,
                                                       json={'enabled': False})).status_code, 403)
            write.assert_not_awaited()
            response = await self.client.put('/admin/users/@Employee', headers=self.auth, json={'enabled': False})
            self.assertEqual(response.status_code, 200)
            write.assert_awaited_once_with(Principal(login='admin'), record)
            response = await self.client.put('/admin/users/employee', headers=self.auth,
                                             json={'enabled': True, 'actor': {'login': 'other'}})
            self.assertEqual(response.status_code, 422)

    async def test_operator_and_public_messages_use_distinct_roles(self):
        reply = TicketMessage(message_id=1, case_id=self.case_id, author_role='operator',
                              author_login='admin', text='Help', created_at=datetime.now(timezone.utc))
        with patch.object(support.repository, 'append_ticket_message', AsyncMock(return_value=reply)) as append:
            response = await self.client.post(f'/admin/tickets/{self.case_id}/messages', headers=self.auth, json=self.body)
            self.assertEqual(response.status_code, 200)
            append.assert_awaited_with(Principal(login='admin'), self.case_id, self.client_id, 'Help')
            await self.client.post(f'/tickets/{self.case_id}/messages', headers=self.auth, json=self.body)
            append.assert_awaited_with(None, self.case_id, self.client_id, 'Help')

    async def test_admin_can_list_close_and_read_metrics(self):
        created = datetime.now(timezone.utc)
        case = SupportCase(case_id=self.case_id, request_id=1, user_id=1, state='open',
                           reason='no_evidence', ticket_url=f'https://bot.example/tickets/{self.case_id}',
                           created_at=created)
        closed = case.model_copy(update={'state': 'closed'})
        metrics = MetricsSnapshot(from_at=created, to_at=created + timedelta(days=1),
                                  requests=1, answered=0, escalated=1, dissatisfied=0,
                                  reply_duration_ms=[], ticket_duration_ms=[])
        actor = Principal(login='admin')
        with patch.object(support.repository, 'read_ticket_list', AsyncMock(return_value=[case])) as read, \
             patch.object(support.repository, 'close_ticket_record', AsyncMock(return_value=closed)) as close, \
             patch.object(support.repository, 'read_metrics', AsyncMock(return_value=metrics)) as stats:
            response = await self.client.get('/tickets?limit=10', headers=self.auth)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()[0]['case_id'], str(self.case_id))
            read.assert_awaited_once_with(actor, 10)
            response = await self.client.post(f'/tickets/{self.case_id}/close', headers=self.auth)
            self.assertEqual((response.status_code, response.json()['state']), (200, 'closed'))
            close.assert_awaited_once_with(actor, self.case_id)
            response = await self.client.get('/admin/metrics', headers=self.auth, params={
                'from_at': metrics.from_at.isoformat(), 'to_at': metrics.to_at.isoformat()})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['escalated'], 1)
            stats.assert_awaited_once_with(actor, metrics.from_at, metrics.to_at)

    async def test_body_limit_applies_to_declared_and_chunked_bodies(self):
        with patch.object(support.repository, 'append_ticket_message', AsyncMock()) as append:
            for path in (f'/tickets/{self.case_id}/messages', f'/admin/tickets/{self.case_id}/messages'):
                response = await self.client.post(path, headers=self.auth, content=b' ' * 32769)
                self.assertEqual(response.status_code, 413)
                async def chunks():
                    yield b' ' * 16000
                    yield b' ' * 17000
                response = await self.client.post(path, headers=self.auth, content=chunks())
                self.assertEqual(response.status_code, 413)
                response = await self.client.post(path, headers=self.auth, json={**self.body, 'text': 'x' * 4097})
                self.assertEqual(response.status_code, 422)
            append.assert_not_awaited()

    async def test_rate_error_and_page_cursor_reach_http(self):
        with patch.object(support.repository, 'append_ticket_message', AsyncMock(side_effect=RateLimited())):
            response = await self.client.post(f'/tickets/{self.case_id}/messages', json=self.body)
            self.assertEqual((response.status_code, response.headers['retry-after']), (429, '60'))
        case = SupportCase(case_id=self.case_id, request_id=1, user_id=1, state='open',
                           reason='no_evidence', ticket_url=f'https://bot.example/tickets/{self.case_id}',
                           created_at=datetime.now(timezone.utc))
        with patch.object(support.repository, 'read_ticket', AsyncMock(return_value=TicketView(
                case=case, history=[], messages=[], next_cursor=40))) as read:
            response = await self.client.get(f'/tickets/{self.case_id}?limit=10&before_message_id=80')
            self.assertEqual(response.status_code, 200)
            self.assertIn('before_message_id=40', response.text)
            read.assert_awaited_once_with(self.case_id, 10, 80)
            for query in ('limit=0', 'limit=101', 'before_message_id=-1', 'before_message_id=9223372036854775808'):
                self.assertEqual((await self.client.get(f'/tickets/{self.case_id}?{query}')).status_code, 422)

    async def test_missing_credentials_and_metrics_validation(self):
        with patch.dict('os.environ', ADMIN_PASSWORD=''):
            self.assertEqual((await self.client.get('/tickets', headers=self.auth)).status_code, 503)
        self.assertEqual((await self.client.get('/admin/metrics?from_at=2026-11-15T00:00:00&to_at=2026-11-16T00:00:00',
                                               headers=self.auth)).status_code, 422)
        response = await self.client.get('/admin', headers=self.auth)
        self.assertEqual(response.status_code, 200)
        self.assertIn('Ответить как оператор', response.text)
        self.assertEqual(response.headers['cache-control'], 'no-store')

    async def test_readiness_maps_actual_admission_result_and_errors(self):
        for result, expected in ((True,200), (False,503), (StorageError(),503)):
            mock = AsyncMock(side_effect=result) if isinstance(result, Exception) else AsyncMock(return_value=result)
            with patch.object(health, 'check_admission_ready', mock):
                self.assertEqual((await self.client.get('/health/ready')).status_code, expected)
        with patch.dict('os.environ', TELEGRAM_BOT_TOKEN=''):
            self.assertEqual((await self.client.get('/health/ready')).status_code, 503)

    async def test_planned_fallback_keeps_variant_and_exact_text(self):
        part = TextPart(kind='text', text=FALLBACK_TEXT, variant='length_fallback')
        with patch.object(sender.adapter, '_post', AsyncMock(return_value=(200, {'ok':True,'result':{'message_id':8}}))):
            sent = await sender._send(1, part)
            self.assertEqual((sent.variant, sent.delivered_text), ('length_fallback', FALLBACK_TEXT))
