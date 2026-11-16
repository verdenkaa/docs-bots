"""Host-side polling for the local Mini App launcher; starts no models or stores."""

import asyncio

import httpx

from deploy.polling import _Fatal, _Poller, _Retry
from rag.rag_api.config import get_settings


async def run():
    # The local launcher owns the process lifetime and refuses a second launch.
    # Production uses deploy.polling.run with its shared Linux spool lock.
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
        poller = _Poller(client, get_settings(), heartbeat=lambda: None,
                         intake_url='http://127.0.0.1:8000/telegram/webhook')
        prepared = False
        while True:
            try:
                if not prepared:
                    await poller.prepare()
                    prepared = True
                await poller.step()
            except _Retry as error:
                print('Local Telegram polling retry: ' + error.code, flush=True)
                await asyncio.sleep(error.delay)
            else:
                await asyncio.sleep(.1)


if __name__ == '__main__':
    try:
        asyncio.run(run())
    except _Fatal as error:
        raise SystemExit(str(error)) from None
