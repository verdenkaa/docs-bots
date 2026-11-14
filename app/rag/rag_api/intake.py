"""Translate trusted Telegram structure to existing application DTOs."""
from collections.abc import Mapping
from datetime import datetime, timezone
import re
from uuid import UUID

from pydantic import ValidationError

from rag.contracts import ImageQuestion, TextQuestion
from rag.rag_api.access import _normalize_username
from rag.rag_api.contracts import IntakeResult, NewQuestion, SupportAction, SupportCommand
from rag.rag_api import support
from rag.rag_api.telegram.adapter import _answer_callback
from rag.rag_api._observability import _step, _call
from rag.runtime.postgresql import rag_api as repository

IMAGE_MIMES = frozenset({'image/jpeg', 'image/png', 'image/webp', 'image/gif'})
# Explicit phrases only; never infer dissatisfaction from arbitrary question tone.
HUMAN_REQUESTS = frozenset({'/support', '/operator', 'позвать оператора',
                            'позови оператора', 'нужен оператор', 'нужна помощь человека'})


def positive(value):
    return type(value) is int and 0 < value <= 2 ** 63 - 1


def parse_action(data: object) -> SupportAction | None:
    if not isinstance(data, str) or len(data.encode('utf-8')) > 64:
        return None
    match = re.fullmatch(r'([as]):([1-9][0-9]{0,18})', data, re.ASCII)
    if match:
        kind, key = match.groups()
        if not positive(int(key)):
            return None
        if kind == 'a':
            return SupportAction(kind='offer', request_id=int(key), reason='dissatisfied')
        return SupportAction(kind='start', request_id=int(key))
    match = re.fullmatch(r'([yn]):([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})', data)
    if match:
        return SupportAction(kind='confirm' if match[1] == 'y' else 'decline', case_id=UUID(match[2]))
    return None


def question_input(message):
    caption = message.get('caption')
    if caption is not None and not isinstance(caption, str):
        raise ValueError('invalid_caption')
    if 'photo' in message:
        photos = message['photo']
        if not isinstance(photos, list) or not photos:
            raise ValueError('invalid_photo')
        valid = [p for p in photos if isinstance(p, dict)
                 and isinstance(p.get('file_id'), str) and p['file_id'].strip()
                 and positive(p.get('width')) and positive(p.get('height'))
                 and ('file_size' not in p or positive(p['file_size']))]
        if not valid:
            raise ValueError('invalid_photo')
        selected = max(valid, key=lambda p: (p['width'] * p['height'],
                       p.get('file_size', 0), p['width'], p['height'], p['file_id']))
        return ImageQuestion(kind='image', file_id=selected['file_id'], caption=caption)
    if 'document' in message:
        document = message['document']
        if not isinstance(document, dict):
            raise ValueError('invalid_document')
        if document.get('mime_type') not in IMAGE_MIMES:
            return None
        return ImageQuestion(kind='image', file_id=document.get('file_id'), caption=caption)
    if 'text' in message:
        return TextQuestion(kind='text', text=message['text'])
    return None


def _parse(update, now):
    callback = update.get('callback_query')
    if 'callback_query' in update:
        if not isinstance(callback, dict):
            raise ValueError('invalid_callback')
        if not isinstance(callback.get('id'), str) or not callback['id'].strip():
            raise ValueError('invalid_callback_id')
        message, sender = callback.get('message', {}), callback.get('from')
        if not message:
            return None, None, 'ignored'
    else:
        if 'message' not in update:
            return None, None, 'ignored'
        message = update['message']
        sender = message.get('from') if isinstance(message, dict) else None
    if not isinstance(message, dict) or not isinstance(message.get('chat'), dict):
        raise ValueError('invalid_message')
    chat = message['chat']
    if chat.get('type') not in ('private', 'group', 'supergroup', 'channel'):
        raise ValueError('invalid_chat_type')
    if chat['type'] != 'private':
        return None, None, 'ignored'
    if not isinstance(sender, dict) or not positive(sender.get('id')) or not positive(chat.get('id')):
        raise ValueError('invalid_identity')
    user_id, chat_id = sender['id'], chat['id']
    common = dict(update_id=update['update_id'], user_id=user_id, chat_id=chat_id,
                  received_at=now, username=_normalize_username(sender.get('username')))
    if callback is not None:
        if not positive(message.get('message_id')):
            raise ValueError('invalid_callback_message')
        action = parse_action(callback.get('data'))
        command = SupportCommand(**common, action=action, origin='callback',
            callback_message_id=message['message_id']) if action else 'ignored'
        return user_id, chat_id, command
    text = message.get('text')
    if isinstance(text, str) and text.strip().lower() in HUMAN_REQUESTS:
        return user_id, chat_id, SupportCommand(**common, origin='text',
            action=SupportAction(kind='offer', reason='human_request'))
    question = question_input(message)
    if question is None:
        return user_id, chat_id, 'ignored'
    # Reject invalid Unicode at the transport boundary, before any DB operation.
    for value in question.model_dump().values():
        if isinstance(value, str):
            value.encode('utf-8')
    return user_id, chat_id, NewQuestion(**common, input=question)


async def _handle(update: Mapping[str, object]) -> IntakeResult:
    if (not isinstance(update, Mapping) or type(update.get('update_id')) is not int
            or not 0 <= update['update_id'] <= 2 ** 63 - 1):
        return IntakeResult(kind='invalid_update')
    now = datetime.now(timezone.utc)
    callback = update.get('callback_query')
    callback_id = callback.get('id') if isinstance(callback, dict) else None
    try:
        try:
            with _step('parse_update', error_code='invalid_update') as step:
                user_id, chat_id, operation = _parse(update, now)
                step['outcome'] = ('question' if isinstance(operation, NewQuestion)
                                   else 'support_action' if isinstance(operation, SupportCommand) else operation)
        except (ValueError, ValidationError):
            return await _call('record_update', repository.record_update, update['update_id'], None, None, now, 'invalid_update')
        # Repository errors are outside the parsing exception boundary: do not
        # turn a programming error or broken stored DTO into a durable 400.
        if isinstance(operation, SupportCommand):
            return await support.handle_support_action(operation)
        if isinstance(operation, NewQuestion):
            # No preliminary Access read: ledger wins over changed admission.
            return await _call('register_question', repository.register_question, operation)
        return await _call('record_update', repository.record_update, update['update_id'], user_id, chat_id, now, operation)
    finally:
        if isinstance(callback_id, str) and callback_id.strip():
            try:
                await _call('answer_callback', _answer_callback, callback_id, _error_code='callback_ack_failed')
            except Exception:
                # The failed step is part of the intake event; a committed action
                # remains committed, and the HTTP decision is unchanged.
                pass
