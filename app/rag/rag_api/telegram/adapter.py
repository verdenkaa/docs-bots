"""Bounded Bot API transport; response descriptions never enter logs."""
import httpx
from rag.rag_api.config import get_settings
from rag.rag_api._observability import _step
from rag.rag_api.delivery.limiter import limiter


async def _post(method: str, payload: dict, *, files=None, chat_id=None):
    settings = get_settings()
    with _step('telegram_rate_limit', error_code='telegram_limiter_error'):
        await limiter.wait(chat_id)
    with _step(method, error_code='telegram_transport_error') as step:
        async with httpx.AsyncClient(timeout=settings.timeout_s, follow_redirects=False, trust_env=False) as client:
            url = settings.telegram_url + '/' + method
            if files is None:
                response = await client.post(url, json=payload)
            else:
                response = await client.post(url, data=payload, files=files)
        step['http_status'] = response.status_code
        try:
            body = response.json()
        except ValueError:
            body = {}
        if not isinstance(body, dict):
            body = {}
        success = response.status_code == 200 and body.get('ok') is True
        step['outcome'] = 'confirmed' if success else 'rejected_or_unknown'
        if type(body.get('error_code')) is int:
            step['telegram_error_code'] = body['error_code']
        if not success:
            step['error_code'] = 'telegram_api_error'
        result = body.get('result')
        if success and method in ('sendMessage', 'sendPhoto'):
            message_id = result.get('message_id') if isinstance(result, dict) else None
            if type(message_id) is int and message_id > 0:
                step['message_id'] = message_id
            else:
                step.update(outcome='unknown', error_code='telegram_receipt_invalid')
        return response.status_code, body


async def _answer_callback(callback_id: str):
    status, result = await _post('answerCallbackQuery', {'callback_query_id': callback_id})
    if status != 200 or result.get('ok') is not True:
        raise RuntimeError('callback_ack_failed')
