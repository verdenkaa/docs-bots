"""Send one saved part, with at most one explicitly rejected-length fallback."""
import httpx
from pydantic import TypeAdapter
from rag.contracts import StepError
from rag.rag_api._observability import _call, _part_scope
from rag.rag_api.contracts import DeliveryPart, SendFailed, Sent
from rag.rag_api.delivery.limiter import limiter
from rag.rag_api.delivery._texts import FALLBACK_TEXT
from rag.rag_api.telegram import adapter
from rag.runtime import media, s3

def keyboard(actions):
    return {'inline_keyboard': [[a.model_dump(exclude_none=True) for a in actions]]}


def failure(retryable=False, uncertain=False, retry_after=None):
    return SendFailed(kind='failed', retryable=retryable, uncertain=uncertain,
                      retry_after_seconds=retry_after)


async def _send(chat_id: int, part: DeliveryPart):
    if type(chat_id) is not int or chat_id <= 0:
        raise ValueError('invalid_chat_id')
    part = TypeAdapter(DeliveryPart).validate_python(part.model_dump())
    files = None
    if part.kind == 'image':
        try:
            image = await _call('get_image', s3.get_image, part.image.image_id, _error_code='image_read_failed')
            photo = await _call('prepare_telegram_photo', media.prepare_telegram_photo, image, _error_code='photo_prepare_failed')
        except StepError as exc:
            return failure(retryable=exc.retryable)
        except (OSError, TimeoutError, httpx.HTTPError):
            return failure(retryable=True)
        method, payload = 'sendPhoto', {'chat_id': str(chat_id)}
        files = {'photo': ('image.jpg', photo.data, 'image/jpeg')}
    else:
        method = 'sendMessage'
        payload = {'chat_id': chat_id, 'text': part.text,
                   'entities': [e.model_dump(exclude_none=True) for e in part.entities]}
        if part.actions:
            payload['reply_markup'] = keyboard(part.actions)
    variant = part.variant if part.kind == 'text' else 'normal'
    for attempt in range(2):
        try:
            fields = {'variant': variant}
            if part.kind == 'text':
                fields['delivery_variant'] = 'length_fallback' if variant == 'length_fallback' else 'answer'
            with _part_scope(**fields):
                status, body = await adapter._post(method, payload, files=files, chat_id=chat_id)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout):
            return failure(retryable=True)
        except (httpx.HTTPError, TimeoutError, OSError):
            return failure(retryable=True, uncertain=True)
        if status == 200 and body.get('ok') is True:
            result = body.get('result')
            message_id = result.get('message_id') if isinstance(result, dict) else None
            if type(message_id) is not int or message_id <= 0:
                return failure(retryable=True, uncertain=True)
            return Sent(kind='sent', message_id=message_id, variant=variant,
                        delivered_text=payload['text'] if part.kind == 'text' else None)
        code = body.get('error_code')
        if status == 429 or code == 429:
            parameters = body.get('parameters')
            delay = parameters.get('retry_after') if isinstance(parameters, dict) else None
            delay = delay if type(delay) is int and delay >= 0 else None
            await limiter.defer(chat_id, delay if delay is not None else 1)
            return failure(retryable=True, retry_after=delay)
        if status >= 500 or (type(code) is int and code >= 500):
            return failure(retryable=True, uncertain=True)
        description = body.get('description', '')
        if (attempt == 0 and part.kind == 'text' and status == 400
                and body.get('ok') is False and code == 400
                and isinstance(description, str)
                and 'message is too long' in description.lower()):
            # The first request was explicitly rejected. A timeout or malformed
            # success receipt must never enter this fallback branch.
            payload = {'chat_id': chat_id, 'text': FALLBACK_TEXT}
            if part.actions:
                payload['reply_markup'] = keyboard(part.actions)
            variant = 'length_fallback'
            continue
        if 400 <= status < 500 or (body.get('ok') is False and type(code) is int and 400 <= code < 500):
            return failure()
        return failure(retryable=True, uncertain=True)
    return failure()
