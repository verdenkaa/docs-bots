"""Regression scenarios found in the RAG API review; no external services."""

import asyncio
import io
import json
import re
import shutil
import subprocess
import unittest
from contextlib import redirect_stdout
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from rag.contracts import DeliveryRef, ImageRef, RagResult, StorageError
from rag.rag_api import entrypoints
from rag.rag_api.contracts import IntakeResult, TextPart
from rag.rag_api.delivery import formatter
from rag.rag_api.delivery.limiter import TelegramLimiter
from rag.rag_api.telegram import router as webhook
from rag.rag_api.tests.test_api import _delivery, _HttpCase, _source, _update


class FormattingRegressions(unittest.TestCase):
    def test_code_and_links_have_compatible_entities_without_losing_text(self):
        samples = [
            ('**`pip install`**', 'pip install', [('code', 0, 11)]),
            ('[`docs`](https://example.org)', 'docs', [('text_link', 0, 4)]),
            ('**😀 `code` end**', '😀 code end',
             [('bold', 0, 3), ('code', 3, 4), ('bold', 7, 4)]),
            ('**use `a**b` here**', 'use a**b here',
             [('bold', 0, 4), ('code', 4, 4), ('bold', 8, 5)]),
        ]
        for raw, expected, spans in samples:
            with self.subTest(raw=raw):
                text, entities = formatter.markdown(raw)
                self.assertEqual(text, expected)
                self.assertEqual([(e.type, e.offset, e.length) for e in entities], spans)
                TextPart(kind='text', text=text, entities=entities)

    def test_code_keeps_comments_whitespace_and_markdown(self):
        code = '# comment\n  ## indented\n\nx = "**literal**"\n'
        text, entities = formatter.markdown('# Heading\n```python\n' + code + '```')
        self.assertEqual(text, 'Heading\n' + code)
        self.assertEqual([(e.type, e.offset, e.length) for e in entities],
                         [('pre', 8, formatter.units(code))])
        self.assertEqual(formatter.markdown('`# comment`')[0], '# comment')
        self.assertEqual(formatter.markdown('**# literal**')[0], '# literal')
        self.assertEqual(formatter.markdown('#\nnext')[0], '#\nnext')

    def test_paths_identifiers_escapes_and_nested_emphasis(self):
        for text in (r'C:\Users\user', 'some_config_value', 'some__config__value'):
            self.assertEqual(formatter.markdown(text), (text, []))
        self.assertEqual(formatter.markdown(r'\*literal\*'), ('*literal*', []))
        self.assertEqual(formatter.markdown(r'**a \** b**')[0], 'a ** b')
        text, entities = formatter.markdown('***important***')
        self.assertEqual(text, 'important')
        self.assertEqual({e.type for e in entities}, {'bold', 'italic'})
        self.assertEqual(formatter.markdown('unclosed **text')[0], 'unclosed **text')

    def test_all_uuid_spellings_resolve_the_same_saved_image(self):
        image = ImageRef(image_id=uuid4(), description='# Diagram [literal]')
        for key in (str(image.image_id), str(image.image_id).upper(),
                    image.image_id.hex, '{' + str(image.image_id) + '}'):
            with self.subTest(key=key):
                result = RagResult(answer_text=f'![diagram](image://{key})',
                                   sources=[_source()], images=[image])
                parts = formatter._build(_delivery(content=result), 'https://bot.example')
                self.assertTrue(parts[0].text.startswith(image.description))
                self.assertEqual(parts[1].image, image)

    def test_whitespace_after_rendering_uses_fallback_and_valid_entities(self):
        for raw in ('** **', '` `', '```\n \n```'):
            with self.subTest(raw=raw):
                result = RagResult(answer_text=raw, sources=[])
                part = formatter._build(_delivery('notice', content=result), 'https://bot.example')[0]
                self.assertEqual(part.text, 'Ответ не содержит текста.')
                self.assertEqual(part.entities, [])
                TextPart.model_validate(part.model_dump())


class LimiterRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_waiting_chat_does_not_block_other_chats_or_defer(self):
        limiter = TelegramLimiter(bot_interval=0, chat_interval=1)
        sleeping = asyncio.Event()
        async def hold(seconds):
            sleeping.set()
            await asyncio.Event().wait()
        with patch('rag.rag_api.delivery.limiter.monotonic', return_value=10.0), \
             patch('rag.rag_api.delivery.limiter.asyncio.sleep', side_effect=hold):
            await limiter.wait(1)
            waiting = asyncio.create_task(limiter.wait(1))
            try:
                await asyncio.wait_for(sleeping.wait(), 1)
                await asyncio.wait_for(limiter.wait(2), 1)
                await asyncio.wait_for(limiter.defer(1, 5), 1)
            finally:
                waiting.cancel()
                await asyncio.gather(waiting, return_exceptions=True)

    async def test_waking_sender_rechecks_new_retry_after(self):
        limiter = TelegramLimiter(bot_interval=0, chat_interval=1)
        now = 10.0
        delays = []
        async def advance(seconds):
            nonlocal now
            delays.append(seconds)
            now += seconds
            if len(delays) == 1:
                await limiter.defer(1, 5)
        with patch('rag.rag_api.delivery.limiter.monotonic', side_effect=lambda: now), \
             patch('rag.rag_api.delivery.limiter.asyncio.sleep', side_effect=advance):
            await limiter.wait(1)
            await limiter.wait(1)
        self.assertEqual(delays, [1, 5])

    async def test_competing_chats_still_respect_bot_interval(self):
        limiter = TelegramLimiter(bot_interval=0.1, chat_interval=1)
        now = 10.0
        async def advance(seconds):
            nonlocal now
            now += seconds
        with patch('rag.rag_api.delivery.limiter.monotonic', side_effect=lambda: now), \
             patch('rag.rag_api.delivery.limiter.asyncio.sleep', side_effect=advance):
            await asyncio.gather(*(limiter.wait(chat) for chat in range(4)))
        self.assertAlmostEqual(now, 10.3)


class DeliveryRegressions(unittest.IsolatedAsyncioTestCase):
    async def test_exhausted_attempt_can_finalize_saved_receipts_without_sending(self):
        delivery = _delivery(parts=[TextPart(kind='text', text='delivered')], next_part=1)
        delivery.attempt.attempt_no = 6
        with redirect_stdout(io.StringIO()), \
             patch.object(entrypoints.repository, 'claim_delivery', AsyncMock(return_value=delivery)), \
             patch.object(entrypoints.repository, 'complete_delivery', AsyncMock(return_value=True)) as complete, \
             patch.object(entrypoints, 'send_answer', AsyncMock()) as send:
            self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'ack')
            complete.assert_awaited_once_with(delivery.attempt, True, False)
            send.assert_not_awaited()

    async def test_finalization_storage_error_remains_retryable_after_limit(self):
        delivery = _delivery(parts=[TextPart(kind='text', text='delivered')], next_part=1)
        delivery.attempt.attempt_no = 6
        with redirect_stdout(io.StringIO()), \
             patch.object(entrypoints.repository, 'claim_delivery', AsyncMock(return_value=delivery)), \
             patch.object(entrypoints.repository, 'complete_delivery', AsyncMock(side_effect=StorageError())), \
             patch.object(entrypoints, 'send_answer', AsyncMock()) as send:
            self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'retry_broker')
            send.assert_not_awaited()


class HttpRegressions(_HttpCase):
    async def test_webhook_checks_declared_and_streamed_limit_before_dispatch(self):
        headers = {'X-Telegram-Intake-Secret': 'test-secret', 'Content-Type': 'application/json'}
        with patch.object(entrypoints, 'handle_telegram_update', AsyncMock()) as handle:
            body = b'x' * (webhook._MAX_UPDATE_BYTES + 1)
            response = await self.client.post('/internal/telegram/updates', content=body, headers=headers)
            self.assertEqual(response.status_code, 413)
            async def chunks():
                yield body[:500000]
                yield body[500000:]
            response = await self.client.post('/internal/telegram/updates', content=chunks(), headers=headers)
            self.assertEqual(response.status_code, 413)
            handle.assert_not_awaited()

    async def test_webhook_accepts_exact_limit_and_rejects_invalid_lengths(self):
        headers = {'X-Telegram-Intake-Secret': 'test-secret', 'Content-Type': 'application/json'}
        body = json.dumps(_update()).encode()
        body += b' ' * (webhook._MAX_UPDATE_BYTES - len(body))
        with patch.object(entrypoints, 'handle_telegram_update', AsyncMock(return_value=IntakeResult(kind='accepted', request_id=1))) as handle:
            self.assertEqual((await self.client.post('/internal/telegram/updates', content=body, headers=headers)).status_code, 200)
            handle.assert_awaited_once_with(_update())
            handle.reset_mock()
            for length in ('bad', '-1', '9' * 5000):
                response = await self.client.post('/internal/telegram/updates', content=b'{}',
                                                  headers={**headers, 'Content-Length': length})
                self.assertEqual(response.status_code, 413 if length[0] == '9' else 400)
            handle.assert_not_awaited()

    async def test_webhook_authenticates_before_reading_body(self):
        read = False
        async def chunks():
            nonlocal read
            read = True
            yield b'x' * (webhook._MAX_UPDATE_BYTES + 1)
        response = await self.client.post('/internal/telegram/updates', content=chunks(),
                                          headers={'Content-Type': 'application/json'})
        self.assertEqual(response.status_code, 403)
        self.assertFalse(read)

    async def test_admin_exposes_lookup_and_operator_actions_for_known_uuid(self):
        import base64
        headers = {'Authorization': 'Basic ' + base64.b64encode(b'admin:test-password').decode()}
        response = await self.client.get('/admin', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertIn('последние 100 тикетов', response.text)
        self.assertIn('id="ticket-lookup"', response.text)
        self.assertIn('id="selected-ticket"', response.text)
        self.assertIn("renderTicket({case_id:id,state:", response.text)
        self.assertIn("if(ticket.state!=='closed')", response.text)

    @unittest.skipUnless(shutil.which('node'), 'Node.js is needed to exercise the panel script')
    async def test_admin_lookup_can_reply_and_close_ticket_outside_recent_list(self):
        import base64
        response = await self.client.get('/admin', headers={
            'Authorization': 'Basic ' + base64.b64encode(b'admin:test-password').decode()})
        script = re.search(r'<script>(.*?)</script>', response.text, re.S)[1]
        # Execute the actual page script with a small DOM/HTTP fixture. The
        # recent list is empty, so the actions must come from UUID lookup.
        harness = r'''
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
class Element {
 constructor(tag) {this.tag=tag;this.children=[];this.value='';}
 append(child) {this.children.push(child);child.parent=this;}
 replaceChildren() {this.children=[];}
 remove() {this.parent.children=this.parent.children.filter(child=>child!==this);}
}
const nodes = new Map();
const document = {
 querySelector(selector) {
  if(!nodes.has(selector)) nodes.set(selector,new Element('root'));
  return nodes.get(selector);
 },
 createElement: tag=>new Element(tag)
};
const requests=[];
const id='12345678-1234-1234-1234-123456789abc';
const fetch=async (path, options)=>{
 requests.push({path,...options});
 return {ok:true,json:async()=>path==='/tickets?limit=100'?[]:
  path.endsWith('/close')?{case_id:id,state:'closed'}:{message_id:1}};
};
vm.runInNewContext(fs.readFileSync(0,'utf8'), {
 document,fetch,crypto:require('node:crypto'),Error,JSON,encodeURIComponent
});
(async()=>{
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(nodes.get('#tickets').children.length,0);
 const input=document.querySelector('#case-id');input.value='invalid';
 const lookup=nodes.get('#ticket-lookup');lookup.onsubmit({preventDefault(){}});
 assert.equal(nodes.has('#selected-ticket'),false);
 input.value=id.toUpperCase();lookup.onsubmit({preventDefault(){}});
 const selected=nodes.get('#selected-ticket');const article=selected.children[0];
 assert.equal(article.children.find(e=>e.tag==='a').href,'/tickets/'+id);
 const form=article.children.find(e=>e.tag==='form');
 const text=form.children.find(e=>e.tag==='textarea');text.value='Operator reply';
 await form.onsubmit({preventDefault(){}});
 const sent=requests.at(-1);
 assert.equal(sent.path,'/admin/tickets/'+id+'/messages');assert.equal(sent.method,'POST');
 assert.equal(JSON.parse(sent.body).text,'Operator reply');
 assert.ok(JSON.parse(sent.body).client_message_id);
 await article.children.find(e=>e.tag==='button').onclick();
 assert.equal(requests.at(-1).path,'/tickets/'+id+'/close');
 assert.equal(selected.children.length,1);
 assert.equal(selected.children[0].children.some(e=>e.tag==='form'),false);
})().catch(error=>{console.error(error);process.exitCode=1;});
'''
        result = subprocess.run([shutil.which('node'), '-e', harness], input=script,
                                capture_output=True, text=True, encoding='utf-8', timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

