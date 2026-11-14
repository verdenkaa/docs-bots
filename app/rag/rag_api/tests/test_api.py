"""Run from app: python -B -m unittest discover -s rag/rag_api/tests -v."""
import asyncio
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
import io
import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx

from rag.contracts import DeliveryAttempt, DeliveryRef, ImageContent, ImageRef, RagResult, SourceRef, StepError, StorageError
from rag.rag_api import access, application, config, entrypoints, intake, public, support
from rag.rag_api.contracts import (AccessRecord, ClaimedDelivery, Conflict, HistoryEntry,
    ImagePart, IntakeResult, NotFound, SendFailed, Sent, SupportAction, SupportCase,
    SupportCommand, TextPart, TicketMessage, TicketView)
from rag.rag_api.delivery import formatter, sender
from rag.rag_api.delivery.limiter import TelegramLimiter
from rag.rag_api.telegram import adapter, webhook


def _now():
    return datetime.now(timezone.utc)


def _source(document=None, name='Инструкция'):
    return SourceRef(chunk_id=uuid4(), document_id=document or uuid4(), revision_id=uuid4(), source_name=name)


def _delivery(purpose='answer', **kwargs):
    data = dict(attempt=DeliveryAttempt(delivery_id=20, attempt_no=1, deadline_at=_now() + timedelta(seconds=60)),
                user_id=7, chat_id=99, request_id=10, purpose=purpose)
    if purpose in ('answer', 'notice'):
        data['content'] = RagResult(answer_text='Ответ **здесь**', sources=[_source()] if purpose == 'answer' else [])
    data.update(kwargs)
    return ClaimedDelivery(**data)


def _update(**message):
    return {'update_id': 123, 'message': {'from': {'id': 7, 'username': 'Employee'},
            'chat': {'id': 99, 'type': 'private'}, 'text': 'Вопрос', **message}}


def _case(state='waiting', **kwargs):
    data = dict(case_id=uuid4(), request_id=10, user_id=7, state=state, reason='human_request')
    if state in ('waiting', 'awaiting_confirmation'):
        data['check_at'] = _now() - timedelta(seconds=1)
    if state in ('open', 'closed'):
        data.update(ticket_url='https://bot.example/tickets/test', created_at=_now())
    if state == 'closed':
        data['closed_at'] = _now()
    data.update(kwargs)
    return SupportCase(**data)


class _HttpCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.enterContext(patch.dict('os.environ', TELEGRAM_BOT_TOKEN='123:test', BOT_TOKEN='',
                                    TELEGRAM_WEBHOOK_SECRET='test-secret', TELEGRAM_WEBHOOK_URL='https://bot.example'))

    async def asyncSetUp(self):
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=application.app), base_url='http://test')
        self.addAsyncCleanup(self.client.aclose)

    async def post_update(self, payload=None, **kwargs):
        headers = kwargs.pop('headers', {'X-Telegram-Bot-Api-Secret-Token': 'test-secret'})
        return await self.client.post('/telegram/webhook', json=payload if payload is not None else _update(), headers=headers, **kwargs)


class ApiTests(_HttpCase):
    async def test_webhook_all_intake_outcomes_and_single_final_event(self):
        for kind in ('accepted', 'duplicate', 'handled', 'busy', 'denied', 'ignored', 'invalid_update', 'retry_later'):
            with self.subTest(kind=kind), patch.object(entrypoints, 'handle_telegram_update', AsyncMock(
                    return_value=IntakeResult(kind=kind, request_id=10 if kind == 'accepted' else None))) as handle:
                self.output.truncate(0)
                self.output.seek(0)
                result = await self.post_update()
                self.assertEqual(result.status_code, {'invalid_update': 400, 'retry_later': 503}.get(kind, 200))
                self.assertEqual(result.content, b'')
                handle.assert_awaited_once_with(_update())
                records = [json.loads(line) for line in self.output.getvalue().splitlines()]
                self.assertEqual(len(records), 1)
                self.assertEqual(records[0]['outcome'], kind)
                self.assertNotIn('Вопрос', self.output.getvalue())

    async def test_webhook_secret_and_json_structure(self):
        with patch.object(entrypoints, 'handle_telegram_update', AsyncMock()) as handle:
            for headers in ({}, {'X-Telegram-Bot-Api-Secret-Token': 'wrong'}):
                self.assertEqual((await self.post_update(headers=headers)).status_code, 403)
            headers = {'X-Telegram-Bot-Api-Secret-Token': 'test-secret', 'Content-Type': 'application/json'}
            for raw in ('{', '[]', 'null', '{"update_id":true}', '{"update_id":"1"}', '{}', '{"update_id":-1}'):
                self.assertEqual((await self.client.post('/telegram/webhook', content=raw, headers=headers)).status_code, 400)
            headers['Content-Type'] = 'text/plain'
            self.assertEqual((await self.client.post('/telegram/webhook', content='{}', headers=headers)).status_code, 400)
            handle.assert_not_awaited()

    async def test_webhook_exception_does_not_ack(self):
        with patch.object(entrypoints, 'handle_telegram_update', AsyncMock(side_effect=RuntimeError('private-secret'))):
            self.assertEqual((await self.post_update()).status_code, 503)
        self.assertNotIn('private-secret', self.output.getvalue())

    async def test_text_and_atomic_registration_do_not_precheck_access(self):
        with patch.object(intake.repository, 'register_question', AsyncMock(return_value=IntakeResult(kind='accepted', request_id=10))) as register, \
             patch.object(access, 'check_access', AsyncMock(side_effect=AssertionError('must not read'))):
            result = await entrypoints.handle_telegram_update(_update())
        self.assertEqual(result.kind, 'accepted')
        question = register.await_args.args[0]
        self.assertEqual((question.user_id, question.chat_id, question.username), (7, 99, 'employee'))
        self.assertEqual(question.input.text, 'Вопрос')
        self.assertEqual(question.received_at.utcoffset(), timedelta(0))

    async def test_images_photo_document_caption_and_deterministic_choice(self):
        variants = [({'photo': [{'file_id': 'small', 'width': 2, 'height': 2},
                              {'file_id': 'large', 'width': 10, 'height': 10}, {'file_id': 'invalid'}],
                      'caption': 'Подпись'}, 'large'),
                    ({'document': {'file_id': 'png', 'mime_type': 'image/png'}, 'caption': 'Подпись'}, 'png'),
                    ({'document': {'file_id': 'gif', 'mime_type': 'image/gif'}, 'caption': 'Подпись'}, 'gif')]
        for message, expected in variants:
            with patch.object(intake.repository, 'register_question', AsyncMock(return_value=IntakeResult(kind='accepted', request_id=1))) as register:
                await entrypoints.handle_telegram_update(_update(**message))
                question = register.await_args.args[0]
                self.assertEqual(question.input.file_id, expected)
                self.assertEqual(question.input.caption, 'Подпись')

    async def test_ignored_updates_and_groups_are_durable(self):
        for update in ({'update_id': 1, 'channel_post': {}},
                       _update(chat={'id': -10, 'type': 'group'}),
                       {'update_id': 1, 'message': {'from': {'id': 7}, 'chat': {'id': 99, 'type': 'private'}, 'sticker': {}}},
                       _update(document={'mime_type': 'application/pdf'})):
            with patch.object(intake.repository, 'record_update', AsyncMock(return_value=IntakeResult(kind='ignored'))) as record, \
                 patch.object(intake.repository, 'register_question', AsyncMock()) as register:
                self.assertEqual((await entrypoints.handle_telegram_update(update)).kind, 'ignored')
                self.assertEqual(record.await_args.args[-1], 'ignored')
                register.assert_not_awaited()

    async def test_missing_username_busy_duplicate_and_db_failure(self):
        for kind in ('busy', 'denied', 'duplicate', 'retry_later'):
            with patch.object(intake.repository, 'register_question', AsyncMock(return_value=IntakeResult(kind=kind))) as register:
                result = await entrypoints.handle_telegram_update(_update(**{'from': {'id': 7}}))
                self.assertEqual(result.kind, kind)
                self.assertIsNone(register.await_args.args[0].username)
        with patch.object(intake.repository, 'register_question', AsyncMock(side_effect=StorageError())):
            self.assertEqual((await entrypoints.handle_telegram_update(_update())).kind, 'retry_later')

    async def test_malformed_private_update_is_recorded(self):
        for message in ({'from': {'id': True}}, {'text': 3}, {'photo': []}, {'caption': 3}):
            with patch.object(intake.repository, 'record_update', AsyncMock(return_value=IntakeResult(kind='invalid_update'))) as record:
                self.assertEqual((await entrypoints.handle_telegram_update(_update(**message))).kind, 'invalid_update')
                record.assert_awaited_once()

    async def test_repository_programming_error_is_not_saved_as_invalid_input(self):
        with patch.object(intake.repository, 'register_question', AsyncMock(side_effect=ValueError('broken_storage_dto'))), \
             patch.object(intake.repository, 'record_update', AsyncMock()) as record:
            with self.assertRaises(ValueError):
                await entrypoints.handle_telegram_update(_update())
            record.assert_not_awaited()

    async def test_accepted_update_survives_revocation_via_runtime_ledger(self):
        ledger = {}
        enabled = True
        async def register(question):
            if question.update_id in ledger:
                return IntakeResult(kind='duplicate', request_id=ledger[question.update_id])
            if not enabled:
                return IntakeResult(kind='denied')
            ledger[question.update_id] = 10
            return IntakeResult(kind='accepted', request_id=10)
        with patch.object(intake.repository, 'register_question', side_effect=register):
            self.assertEqual((await entrypoints.handle_telegram_update(_update())).kind, 'accepted')
            enabled = False
            self.assertEqual((await entrypoints.handle_telegram_update(_update())).kind, 'duplicate')
            fresh = _update()
            fresh['update_id'] += 1
            self.assertEqual((await entrypoints.handle_telegram_update(fresh)).kind, 'denied')

    async def test_callback_through_http_has_one_ack_and_runtime_authorization(self):
        update = {'update_id': 1, 'callback_query': {'id': 'cq', 'from': {'id': 8, 'username': 'Other'},
                  'message': {'message_id': 5, 'chat': {'id': 99, 'type': 'private'}}, 'data': 's:10'}}
        # Runtime owns ownership/staleness checks; its ignored decision is durable.
        with patch.object(support.repository, 'record_support_action', AsyncMock(return_value=IntakeResult(kind='ignored'))) as record, \
             patch.object(intake, '_answer_callback', AsyncMock()) as ack:
            self.assertEqual((await self.post_update(update)).status_code, 200)
            command = record.await_args.args[0]
            self.assertEqual(command.user_id, 8)
            self.assertEqual(command.callback_message_id, 5)
            ack.assert_awaited_once_with('cq')

    async def test_support_storage_failure_is_retry_even_for_ignored_update(self):
        with patch.object(intake.repository, 'record_update', AsyncMock(side_effect=StorageError())):
            self.assertEqual((await entrypoints.handle_telegram_update({'update_id': 1})).kind, 'retry_later')

    async def test_access_normalization_enabled_disabled_and_storage(self):
        for value, normalized in (('@EMPLOYEE', 'employee'), ('employee', 'employee'), (None, None), ('юзер', None), ('@@abc', None)):
            with patch.object(access.repository, 'read_user_access', AsyncMock(return_value=None)) as read:
                self.assertIsNone(await access.check_access(value))
                read.assert_awaited_once_with(normalized)
        for enabled in (True, False):
            record = AccessRecord(username='employee', enabled=enabled)
            with patch.object(access.repository, 'read_user_access', AsyncMock(return_value=record)):
                self.assertEqual(await access.check_access('EMPLOYEE'), record if enabled else None)
        with patch.object(access.repository, 'read_user_access', AsyncMock(side_effect=StorageError())):
            with self.assertRaises(StorageError):
                await access.check_access('employee')

    async def test_callbacks_are_owned_by_intake_even_when_ack_fails(self):
        case_id = uuid4()
        for data, kind in (('a:10', 'offer'), ('s:10', 'start'), (f'y:{case_id}', 'confirm'), (f'n:{case_id}', 'decline')):
            callback = {'update_id': 1, 'callback_query': {'id': 'cq', 'from': {'id': 7, 'username': 'EMPLOYEE'},
                        'message': {'message_id': 5, 'chat': {'id': 99, 'type': 'private'}}, 'data': data}}
            with patch.object(support, 'handle_support_action', AsyncMock(return_value=IntakeResult(kind='handled'))) as action, \
                 patch.object(intake, '_answer_callback', AsyncMock(side_effect=RuntimeError('secret'))) as ack:
                self.assertEqual((await entrypoints.handle_telegram_update(callback)).kind, 'handled')
                command = action.await_args.args[0]
                self.assertEqual((command.action.kind, command.origin, command.callback_message_id), (kind, 'callback', 5))
                self.assertEqual((command.user_id, command.chat_id), (7, 99))
                ack.assert_awaited_once_with('cq')

    async def test_support_actions_pass_runtime_outcomes_and_original_timer(self):
        command = SupportCommand(update_id=1, user_id=7, chat_id=99, username='employee',
            origin='callback', callback_message_id=5, received_at=_now(), action=SupportAction(kind='start', request_id=10))
        for kind in ('handled', 'ignored', 'denied', 'duplicate', 'retry_later'):
            with patch.object(support.repository, 'record_support_action', AsyncMock(return_value=IntakeResult(kind=kind))) as record:
                self.assertEqual((await support.handle_support_action(command)).kind, kind)
                record.assert_awaited_once_with(command, command.received_at + timedelta(minutes=10))
        with patch.object(support.repository, 'record_support_action', AsyncMock(side_effect=StorageError())):
            self.assertEqual((await support.handle_support_action(command)).kind, 'retry_later')

    async def test_text_operator_request_is_offer_not_question(self):
        with patch.object(support, 'handle_support_action', AsyncMock(return_value=IntakeResult(kind='handled'))) as action:
            await entrypoints.handle_telegram_update(_update(text='/support'))
            command = action.await_args.args[0]
            self.assertEqual((command.origin, command.action.kind, command.action.reason), ('text', 'offer', 'human_request'))
            self.assertIsNone(command.action.request_id)

    async def test_support_maintenance_due_and_automatic_creation(self):
        waiting, creating = _case(), _case('creating')
        with patch.object(support.repository, 'list_pending_support', AsyncMock(return_value=[waiting, creating])), \
             patch.object(support.repository, 'queue_support_confirmation', AsyncMock(return_value=True)) as confirmation, \
             patch.object(support.repository, 'create_support_ticket', AsyncMock(return_value=_case('open', case_id=creating.case_id))) as create:
            self.assertEqual(await support.run_support_batch(5), 2)
            self.assertEqual(confirmation.await_args.args[0], waiting.case_id)
            create.assert_awaited_once_with(creating.case_id, f'https://bot.example/tickets/{creating.case_id}')
        with patch.object(entrypoints.repository, 'queue_waiting_statuses', AsyncMock(return_value=2)) as statuses, \
             patch.object(support, 'run_support_batch', AsyncMock(return_value=3)):
            self.assertEqual(await entrypoints.run_api_maintenance(7), 5)
            self.assertEqual(statuses.await_args.args[1], 7)
            self.assertEqual(statuses.await_args.args[0].utcoffset(), timedelta(0))
        for invalid in (0, -1, True):
            with self.assertRaises(ValueError):
                await entrypoints.run_api_maintenance(invalid)

    async def test_health_and_lifecycle_do_not_manage_webhook_or_consumers(self):
        self.assertEqual((await self.client.get('/health/live')).status_code, 200)
        self.assertEqual((await self.client.get('/health/ready')).status_code, 503)
        with patch.object(application, 'start_runtime', AsyncMock()) as start, \
             patch.object(application, 'close_runtime', AsyncMock()) as close, \
             patch.object(webhook, '_register_webhook', AsyncMock()) as register:
            with self.assertRaises(RuntimeError):
                async with application._lifespan(application.app):
                    raise RuntimeError('body failure')
            start.assert_awaited_once()
            close.assert_awaited_once()
            register.assert_not_awaited()

    async def test_partial_lifecycle_startup_also_closes_runtime(self):
        with patch.object(application, 'start_runtime', AsyncMock(side_effect=RuntimeError('startup'))), \
             patch.object(application, 'close_runtime', AsyncMock()) as close:
            with self.assertRaises(RuntimeError):
                async with application._lifespan(application.app):
                    self.fail('must not start')
            close.assert_awaited_once()

    async def test_explicit_webhook_management_keeps_callbacks_and_pending_updates(self):
        client = AsyncMock()
        with patch.object(webhook, '_telegram_api_request', AsyncMock()) as request:
            await webhook._register_webhook(client)
            payload = request.await_args.args[2]
            self.assertEqual(payload['allowed_updates'], ['message', 'callback_query'])
            self.assertFalse(payload['drop_pending_updates'])
            self.assertEqual(payload['url'], 'https://bot.example/telegram/webhook')
            await webhook._unregister_webhook(client)
            self.assertEqual(request.await_args.args[1:], ('deleteWebhook', {'drop_pending_updates': False}))


class FormatterTests(unittest.TestCase):
    def test_markdown_utf16_nested_styles_and_safe_links(self):
        text, entities = formatter.markdown('😀 **жирный _курсив_** [сайт](https://example.org) `x*y`')
        self.assertEqual(text, '😀 жирный курсив сайт x*y')
        self.assertEqual(entities[0].offset, 3)
        self.assertEqual(entities[0].length, 13)
        self.assertEqual({e.type for e in entities}, {'bold', 'italic', 'text_link', 'code'})
        self.assertEqual(formatter.markdown('[x](javascript:bad)')[1], [])
        self.assertEqual(formatter.markdown('[x](https://[)')[1], [])

    def test_exact_limit_overflow_and_surrogate_safe_truncation(self):
        plain = _delivery('notice', content=RagResult(answer_text='x' * 4096, sources=[]))
        self.assertEqual(len(entrypoints.build_delivery_parts(plain, 'https://bot.example')[0].text), 4096)
        long = _delivery(content=RagResult(answer_text='**' + '😀' * 3000 + '**', sources=[_source()]))
        part = entrypoints.build_delivery_parts(long, 'https://bot.example')[0]
        self.assertLessEqual(formatter.units(part.text), 4096)
        self.assertIn('Источники:', part.text)
        TextPart.model_validate(part.model_dump())

    def test_sources_are_grouped_by_document_and_use_only_trusted_base(self):
        document = uuid4()
        source = _source(document)
        source.source_url = 'https://untrusted.example'
        delivery = _delivery(content=RagResult(answer_text='Ответ', sources=[source, _source(document)]))
        part = entrypoints.build_delivery_parts(delivery, 'https://bot.example')[0]
        links = [e.url for e in part.entities if e.type == 'text_link']
        self.assertEqual(links, [f'https://bot.example/sources/{document}'])
        for url in ('http://bot.example', 'https://user:pass@bot.example', 'https://bot.example?token=secret'):
            with self.assertRaises(ValueError):
                entrypoints.build_delivery_parts(delivery, url)

    def test_images_use_saved_ids_and_description(self):
        image = ImageRef(image_id=uuid4(), description='Схема [согласования]')
        delivery = _delivery(content=RagResult(answer_text=f'Смотрите ![alt](image://{image.image_id})', sources=[_source()], images=[image]))
        parts = entrypoints.build_delivery_parts(delivery, 'https://bot.example')
        self.assertIn(image.description, parts[0].text)
        self.assertEqual(parts[1].image, image)
        delivery.content.answer_text = f'![x](image://{uuid4()})'
        with self.assertRaises(ValueError):
            entrypoints.build_delivery_parts(delivery, 'https://bot.example')

    def test_all_purposes_buttons_and_preserved_plan(self):
        for purpose in ('answer', 'notice', 'status', 'busy', 'denied', 'first_question', 'support_offer', 'support_confirmation', 'ticket_link'):
            extras = {'case': _case('open' if purpose == 'ticket_link' else 'awaiting_confirmation')} if purpose in ('ticket_link', 'support_confirmation') else {}
            part = entrypoints.build_delivery_parts(_delivery(purpose, **extras), 'https://bot.example')[0]
            self.assertTrue(part.text)
            if purpose == 'answer':
                self.assertEqual(part.actions[0].callback_data, 'a:10')
            elif purpose == 'support_offer':
                self.assertEqual(part.actions[0].callback_data, 's:10')
            elif purpose == 'support_confirmation':
                self.assertEqual([a.callback_data[0] for a in part.actions], ['y', 'n'])
            for action in part.actions:
                if action.callback_data:
                    self.assertLessEqual(len(action.callback_data.encode()), 64)
        saved = [TextPart(kind='text', text='saved')]
        self.assertEqual(entrypoints.build_delivery_parts(_delivery(parts=saved, next_part=1), 'https://bot.example'), saved)

    def test_strict_callback_parser(self):
        for data in ('a:0', 's:-1', 'a:true', 'a:10:other', 's:01', 'y:invalid', 'x:1', ' a:1', 'a:１', 's:9999999999999999999'):
            self.assertIsNone(intake.parse_action(data))


class SenderTests(unittest.IsolatedAsyncioTestCase):
    async def test_text_uses_entities_keyboard_and_no_parse_mode(self):
        part = entrypoints.build_delivery_parts(_delivery(), 'https://bot.example')[0]
        with patch.object(adapter, '_post', AsyncMock(return_value=(200, {'ok': True, 'result': {'message_id': 11}}))) as post:
            result = await entrypoints.send_answer(99, part)
            self.assertEqual(result, Sent(kind='sent', message_id=11))
            method, payload = post.await_args.args
            self.assertEqual(method, 'sendMessage')
            self.assertNotIn('parse_mode', payload)
            self.assertTrue(payload['entities'])
            self.assertEqual(payload['reply_markup']['inline_keyboard'][0][0]['callback_data'], 'a:10')

    async def test_photo_uses_runtime_get_prepare_and_jpeg(self):
        image = ImageRef(image_id=uuid4(), description='Схема')
        with patch.object(sender.s3, 'get_image', AsyncMock(return_value=ImageContent(data=b'original', mime_type='image/png'))) as get, \
             patch.object(sender.media, 'prepare_telegram_photo', AsyncMock(return_value=ImageContent(data=b'jpeg', mime_type='image/jpeg'))) as prepare, \
             patch.object(adapter, '_post', AsyncMock(return_value=(200, {'ok': True, 'result': {'message_id': 12}}))) as post:
            self.assertEqual((await sender._send(99, ImagePart(kind='image', image=image))).message_id, 12)
            get.assert_awaited_once_with(image.image_id)
            self.assertEqual(prepare.await_args.args[0].data, b'original')
            self.assertEqual(post.await_args.args[0], 'sendPhoto')
            self.assertEqual(post.await_args.kwargs['files']['photo'], ('image.jpg', b'jpeg', 'image/jpeg'))

    async def test_api_failures_and_retry_after(self):
        variants = [(429, {'ok': False, 'parameters': {'retry_after': 12}}, True, False, 12),
                    (500, {}, True, True, None), (403, {'ok': False}, False, False, None),
                    (200, {'ok': True, 'result': {'message_id': True}}, True, True, None)]
        for status, body, retry, uncertain, delay in variants:
            with patch.object(adapter, '_post', AsyncMock(return_value=(status, body))), \
                 patch.object(sender.limiter, 'defer', AsyncMock()):
                result = await sender._send(99, TextPart(kind='text', text='x'))
                self.assertEqual((result.retryable, result.uncertain, result.retry_after_seconds), (retry, uncertain, delay))

    async def test_network_unknown_result_never_falls_back(self):
        for exception, uncertain in ((httpx.ReadTimeout('token'), True), (httpx.ConnectTimeout('token'), False)):
            with patch.object(adapter, '_post', AsyncMock(side_effect=exception)) as post:
                result = await sender._send(99, TextPart(kind='text', text='x'))
                self.assertEqual(result.uncertain, uncertain)
                post.assert_awaited_once()

    async def test_s3_failure_does_not_call_telegram(self):
        for retry in (True, False):
            with patch.object(sender.s3, 'get_image', AsyncMock(side_effect=StepError('image_unavailable', retry))), \
                 patch.object(adapter, '_post', AsyncMock()) as post:
                result = await sender._send(99, ImagePart(kind='image', image=ImageRef(image_id=uuid4(), description='x')))
                self.assertEqual((result.retryable, result.uncertain), (retry, False))
                post.assert_not_awaited()

    async def test_photo_conversion_failure_is_definite_and_does_not_send(self):
        with patch.object(sender.s3, 'get_image', AsyncMock(return_value=ImageContent(data=b'png', mime_type='image/png'))), \
             patch.object(sender.media, 'prepare_telegram_photo', AsyncMock(side_effect=StepError('telegram_photo_invalid', False))), \
             patch.object(adapter, '_post', AsyncMock()) as post:
            result = await sender._send(99, ImagePart(kind='image', image=ImageRef(image_id=uuid4(), description='x')))
            self.assertEqual((result.retryable, result.uncertain), (False, False))
            post.assert_not_awaited()

    async def test_explicit_length_rejection_allows_exactly_one_fallback(self):
        rejection = (400, {'ok': False, 'error_code': 400, 'description': 'Bad Request: message is too long'})
        with patch.object(adapter, '_post', AsyncMock(side_effect=[rejection, (200, {'ok': True, 'result': {'message_id': 7}})])) as post:
            result = await sender._send(99, TextPart(kind='text', text='x'))
            self.assertEqual(result.variant, 'length_fallback')
            self.assertEqual(post.await_count, 2)
        with patch.object(adapter, '_post', AsyncMock(return_value=rejection)) as post:
            self.assertFalse((await sender._send(99, TextPart(kind='text', text='x'))).retryable)
            self.assertEqual(post.await_count, 2)

    async def test_limiter_has_distinct_bot_and_chat_pacing(self):
        limiter = TelegramLimiter(bot_interval=0.01, chat_interval=0.1)
        with patch('rag.rag_api.delivery.limiter.monotonic', return_value=10.0), \
             patch('rag.rag_api.delivery.limiter.asyncio.sleep', AsyncMock()) as sleep:
            await limiter.wait(1)
            await limiter.wait(2)
            self.assertAlmostEqual(sleep.await_args.args[0], 0.01)
            await limiter.wait(1)
            self.assertAlmostEqual(sleep.await_args.args[0], 0.1)


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(redirect_stdout(io.StringIO()))
        self.enterContext(patch.object(entrypoints, 'get_settings', return_value=type('Settings', (), {'public_url': 'https://bot.example'})()))
        self.parts = [TextPart(kind='text', text='one'), TextPart(kind='text', text='two')]
        self.claim = self.enterContext(patch.object(entrypoints.repository, 'claim_delivery', AsyncMock(return_value=_delivery())))
        self.store = self.enterContext(patch.object(entrypoints.repository, 'store_delivery_parts', AsyncMock(return_value=True)))
        self.record = self.enterContext(patch.object(entrypoints.repository, 'record_delivery_part', AsyncMock(return_value=True)))
        self.retry = self.enterContext(patch.object(entrypoints.repository, 'schedule_delivery_retry', AsyncMock(return_value=True)))
        self.complete = self.enterContext(patch.object(entrypoints.repository, 'complete_delivery', AsyncMock(return_value=True)))
        self.build = self.enterContext(patch.object(entrypoints, 'build_delivery_parts', return_value=self.parts))
        self.send = self.enterContext(patch.object(entrypoints, 'send_answer', AsyncMock(return_value=Sent(kind='sent', message_id=77))))

    async def test_plan_saved_before_send_and_receipts_before_complete(self):
        calls = []
        async def store(*args):
            calls.append('store')
            return True
        async def send(*args):
            calls.append('send')
            return Sent(kind='sent', message_id=77)
        async def record(*args):
            calls.append('record')
            return True
        self.store.side_effect, self.send.side_effect, self.record.side_effect = store, send, record
        self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'ack')
        self.assertEqual(calls, ['store', 'send', 'record', 'send', 'record'])
        self.complete.assert_awaited_once_with(self.claim.return_value.attempt, True, False)

    async def test_existing_plan_resumes_without_rebuilding_or_repeating_text(self):
        self.claim.return_value = _delivery(parts=self.parts, next_part=1)
        self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'ack')
        self.build.assert_not_called()
        self.store.assert_not_awaited()
        self.send.assert_awaited_once_with(99, self.parts[1])
        self.assertEqual(self.record.await_args.args[1], 1)
        self.claim.return_value = None
        await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20))
        self.assertEqual(self.send.await_count, 1)

    async def test_retry_permanent_unknown_and_exhaustion(self):
        for retryable, uncertain in ((True, False), (False, False), (True, True), (False, True)):
            self.retry.reset_mock()
            self.complete.reset_mock()
            self.send.return_value = SendFailed(kind='failed', retryable=retryable, uncertain=uncertain, retry_after_seconds=90)
            before = _now()
            self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'ack')
            if retryable:
                self.assertGreaterEqual(self.retry.await_args.args[1], before + timedelta(seconds=90))
                self.assertEqual(self.retry.await_args.args[2], uncertain)
            else:
                self.complete.assert_awaited_once_with(self.claim.return_value.attempt, False, uncertain)
        self.claim.return_value.attempt.attempt_no = 5
        self.send.return_value = SendFailed(kind='failed', retryable=True, uncertain=True)
        self.retry.reset_mock()
        await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20))
        self.retry.assert_not_awaited()
        self.assertEqual(self.complete.await_args.args[1:], (False, True))

    async def test_stale_attempt_stops_sending(self):
        self.store.return_value = False
        self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'ack')
        self.send.assert_not_awaited()
        self.store.return_value = True
        self.record.return_value = False
        await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20))
        self.send.assert_awaited_once()
        self.complete.assert_not_awaited()

    async def test_storage_failure_requires_broker_retry_at_every_boundary(self):
        for operation in (self.claim, self.store, self.record, self.complete):
            operation.side_effect = StorageError()
            self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'retry_broker')
            operation.side_effect = None

    async def test_expired_lease_never_sends(self):
        self.claim.return_value = _delivery(parts=self.parts)
        self.claim.return_value.attempt.deadline_at = _now() - timedelta(seconds=1)
        self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'retry_broker')
        self.send.assert_not_awaited()

    async def test_lease_timeout_does_not_save_false_receipt(self):
        self.claim.return_value = _delivery(parts=self.parts)
        self.claim.return_value.attempt.deadline_at = _now() + timedelta(seconds=0.02)
        async def slow_send(*args):
            await asyncio.sleep(1)
        self.send.side_effect = slow_send
        self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'retry_broker')
        self.record.assert_not_awaited()
        self.complete.assert_not_awaited()

    async def test_retry_state_write_failure_requires_broker_retry(self):
        self.send.return_value = SendFailed(kind='failed', retryable=True, uncertain=True)
        self.retry.side_effect = StorageError()
        self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'retry_broker')

    async def test_attempt_over_limit_does_not_send_more(self):
        self.claim.return_value.attempt.attempt_no = 6
        self.assertEqual(await entrypoints.handle_rag_result(DeliveryRef(delivery_id=20)), 'ack')
        self.send.assert_not_awaited()
        self.complete.assert_awaited_once_with(self.claim.return_value.attempt, False, False)


class PublicTests(_HttpCase):
    # Reuse setup without inheriting the intake test methods.
    async def test_public_ticket_html_closed_state_and_escaping(self):
        for state in ('open', 'closed'):
            case = _case(state)
            view = TicketView(case=case, history=[HistoryEntry(request_id=10, question_text='<script>alert(1)</script>',
                answer_text='<img src=x>', outcome='answered', received_at=_now())], messages=[])
            with patch.object(support.repository, 'read_ticket', AsyncMock(return_value=view)):
                response = await self.client.get(f'/tickets/{case.case_id}')
                self.assertEqual(response.status_code, 200)
                self.assertIn('&lt;script&gt;', response.text)
                self.assertNotIn('<img src=x>', response.text)
                self.assertEqual('<textarea' in response.text, state == 'open')

    async def test_public_message_never_accepts_actor_and_maps_conflicts(self):
        case_id, client_id = uuid4(), uuid4()
        body = {'client_message_id': str(client_id), 'text': 'Сообщение'}
        message = TicketMessage(message_id=1, case_id=case_id, author_role='visitor', text=body['text'], created_at=_now())
        with patch.object(support.repository, 'append_ticket_message', AsyncMock(return_value=message)) as append:
            for _ in range(2):
                self.assertEqual((await self.client.post(f'/tickets/{case_id}/messages', json=body)).json()['message_id'], 1)
            append.assert_awaited_with(None, case_id, client_id, body['text'])
            for extra in ({'author_role': 'operator'}, {'actor': {'login': 'admin'}}, {'text': '   '}):
                self.assertEqual((await self.client.post(f'/tickets/{case_id}/messages', json={**body, **extra})).status_code, 422)
        for exception, code in ((Conflict(), 409), (NotFound(), 404), (StorageError(), 503)):
            with patch.object(support.repository, 'append_ticket_message', AsyncMock(side_effect=exception)):
                self.assertEqual((await self.client.post(f'/tickets/{case_id}/messages', json=body)).status_code, code)

    async def test_unknown_invalid_and_uncreated_ticket(self):
        for exception, code in ((NotFound(), 404), (StorageError(), 503)):
            with patch.object(support.repository, 'read_ticket', AsyncMock(side_effect=exception)):
                self.assertEqual((await self.client.get(f'/tickets/{uuid4()}')).status_code, code)
        self.assertEqual((await self.client.get('/tickets/bad-uuid')).status_code, 422)
        self.assertEqual((await self.client.get('/tickets')).status_code, 404)
        self.assertEqual((await self.client.get('/admin/metrics')).status_code, 404)

    async def test_source_uses_metadata_escapes_title_and_ignores_query(self):
        from rag.runtime.media import SourceMetadata
        key = uuid4()
        with patch.object(public.media, 'load_source_metadata', AsyncMock(return_value=SourceMetadata(document_id=key, source_name='<img>'))) as load:
            response = await self.client.get(f'/sources/{key}?source_name=evil')
            self.assertEqual(response.status_code, 200)
            self.assertIn('&lt;img&gt;', response.text)
            self.assertNotIn('evil', response.text)
            load.assert_awaited_once_with(key)
        with patch.object(public.media, 'load_source_metadata', AsyncMock(return_value=None)):
            self.assertEqual((await self.client.get(f'/sources/{key}')).status_code, 404)
        with patch.object(public.media, 'load_source_metadata', AsyncMock(side_effect=StorageError())):
            self.assertEqual((await self.client.get(f'/sources/{key}')).status_code, 503)
        self.assertEqual((await self.client.get('/sources/not-uuid')).status_code, 422)


class SettingsTests(unittest.TestCase):
    def test_environment_only_canonical_token_and_secret_validation(self):
        with patch.dict('os.environ', TELEGRAM_BOT_TOKEN='123:test', BOT_TOKEN='', TELEGRAM_WEBHOOK_SECRET='secret', TELEGRAM_WEBHOOK_URL='https://bot.example'):
            settings = config.get_settings()
            self.assertEqual(settings.token, '123:test')
            self.assertNotIn('123:test', repr(settings))
            with patch.dict('os.environ', BOT_TOKEN='456:ignored'):
                self.assertEqual(config.get_settings().token, '123:test')
            for env in ({'TELEGRAM_WEBHOOK_SECRET': 'bad secret'},
                        {'TELEGRAM_WEBHOOK_URL': 'http://example.org'}, {'TELEGRAM_BOT_TOKEN': ''}):
                with patch.dict('os.environ', env), self.assertRaises(ValueError):
                    config.get_settings()
