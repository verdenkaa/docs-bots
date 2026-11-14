"""Process-local bot/chat pacing. Deploy one results process per bot.

This is deliberately not a distributed rate limiter. Entries for inactive chats
expire on each call, so the dictionary cannot grow with all historical users.
"""
import asyncio
from time import monotonic


class TelegramLimiter:
    def __init__(self, bot_interval=1 / 25, chat_interval=1.0):
        self.bot_interval = bot_interval
        self.chat_interval = chat_interval
        self._lock = asyncio.Lock()
        self._bot_at = 0.0
        self._chat_at = {}

    async def wait(self, chat_id=None):
        async with self._lock:
            now = monotonic()
            self._chat_at = {key: at for key, at in self._chat_at.items() if at > now}
            delay = max(0, self._bot_at - now, self._chat_at.get(chat_id, 0) - now)
            if delay:
                await asyncio.sleep(delay)
            now = monotonic()
            self._bot_at = now + self.bot_interval
            if chat_id is not None:
                self._chat_at[chat_id] = now + self.chat_interval

    async def defer(self, chat_id, seconds):
        async with self._lock:
            until = monotonic() + seconds
            self._bot_at = max(self._bot_at, until)
            if chat_id is not None:
                self._chat_at[chat_id] = max(self._chat_at.get(chat_id, 0), until)


limiter = TelegramLimiter()
