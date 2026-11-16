"""Telegram long polling transport; intake, persistence and delivery stay in RAG API."""

import asyncio
from contextlib import contextmanager
import json
import os
from pathlib import Path
import signal
import time

import httpx

from rag.rag_api.config import TELEGRAM_INTAKE_PATH, get_settings

_STATUS = Path('/tmp/telegram-poller-status.json')
_LOCK = Path('/var/lib/rag/spool/telegram-poller.lock')


class _Retry(Exception):
    def __init__(self, code, delay=3):
        self.code, self.delay = code, delay


class _Fatal(RuntimeError):
    pass


def healthy():
    try:
        value = json.loads(_STATUS.read_text())
        os.kill(value['pid'], 0)
        return 0 <= time.time() - value['updated'] < 90
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _heartbeat():
    temporary = _STATUS.with_suffix('.tmp')
    temporary.write_text(json.dumps(dict(pid=os.getpid(), updated=time.time())))
    os.replace(temporary, _STATUS)


@contextmanager
def _exclusive():
    import fcntl
    # Stable inode in the shared spool: never unlink this file on shutdown.
    with _LOCK.open('a') as file:
        try:
            fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise _Fatal('Another Telegram poller owns the shared spool lock') from None
        yield


class _Poller:
    def __init__(self, client, settings, heartbeat=_heartbeat):
        self.client, self.settings, self.heartbeat = client, settings, heartbeat
        self.offset = None

    async def _api(self, method, payload):
        try:
            response = await self.client.post(self.settings.telegram_url + '/' + method,
                                              json=payload, timeout=40)
            if response.status_code in (401, 403, 409):
                raise _Fatal('Telegram polling: ' + str(response.status_code) + ' (credentials or competing receiver)')
            if response.status_code == 429:
                try:
                    delay = int(response.json().get('parameters', {}).get('retry_after', 5))
                except (ValueError, TypeError, AttributeError):
                    delay = 5
                raise _Retry('telegram_rate_limited', max(1, min(delay, 300)))
            if response.status_code != 200:
                raise _Retry('telegram_http_' + str(response.status_code))
            value = response.json()
            if not isinstance(value, dict) or value.get('ok') is not True:
                raise _Retry('telegram_invalid_response')
            return value.get('result')
        except httpx.HTTPError:
            raise _Retry('telegram_connection_failed') from None
        except (ValueError, TypeError):
            raise _Retry('telegram_invalid_response') from None

    async def prepare(self):
        if await self._api('deleteWebhook', {'drop_pending_updates': False}) is not True:
            raise _Retry('telegram_webhook_not_removed')
        print('Telegram polling enabled; webhook removed without dropping updates', flush=True)

    async def step(self):
        payload = dict(timeout=30, limit=100, allowed_updates=['message', 'callback_query'])
        if self.offset is not None:
            payload['offset'] = self.offset
        updates = await self._api('getUpdates', payload)
        if not isinstance(updates, list) or len(updates) > 100:
            raise _Retry('telegram_invalid_updates')
        ids = [item.get('update_id') if isinstance(item, dict) else None for item in updates]
        if (any(type(number) is not int or not 0 <= number <= 2**63-1 for number in ids)
                or len(set(ids)) != len(ids)):
            raise _Retry('telegram_invalid_update_ids')
        for update in sorted(updates, key=lambda item: item['update_id']):
            # Skip acknowledged entries if the API replays part of a batch.
            if self.offset is not None and update['update_id'] < self.offset:
                continue
            try:
                response = await self.client.post('http://core:8000' + TELEGRAM_INTAKE_PATH,
                    json=update, timeout=15,
                    headers={'X-Telegram-Intake-Secret': self.settings.intake_secret})
            except httpx.HTTPError:
                raise _Retry('polling_intake_unavailable') from None
            if response.status_code in (400, 401, 403, 404, 422):
                raise _Fatal('Polling intake rejected update_id=' + str(update['update_id'])
                             + ', HTTP=' + str(response.status_code) + '; update NOT acknowledged')
            if response.status_code != 200:
                raise _Retry('polling_intake_http_' + str(response.status_code))
            # HTTP 200 means the existing handler completed durable intake or
            # classified the update as ignored/duplicate. Only now may we ack it.
            self.offset = update['update_id'] + 1
            self.heartbeat()
            print('Telegram update accepted: update_id=' + str(update['update_id']), flush=True)
        self.heartbeat()


async def run():
    _STATUS.unlink(missing_ok=True)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(signum, stop.set)
    try:
        with _exclusive():
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                poller = _Poller(client, get_settings())
                prepared = False
                while not stop.is_set():
                    try:
                        if not prepared:
                            await poller.prepare()
                            prepared = True
                        await poller.step()
                        delay = .1
                    except _Retry as error:
                        print('Telegram polling retry: ' + error.code, flush=True)
                        delay = error.delay
                    try:
                        await asyncio.wait_for(stop.wait(), timeout=delay)
                    except TimeoutError:
                        pass
    except _Fatal as error:
        raise RuntimeError(str(error)) from None
    finally:
        _STATUS.unlink(missing_ok=True)
