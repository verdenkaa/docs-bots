"""Contracts: docs/documents/infrastructure/redis/contracts.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from pydantic import Field

from rag.contracts import RagResult
from rag.dto import DTO, NonNegativeInt

import asyncio
import hashlib
from uuid import uuid4
from redis.exceptions import RedisError, WatchError
from pydantic import ValidationError
from rag.runtime._state import _get
from rag.runtime._validation import _text


class CacheUnavailable(Exception):
    """Операция Redis недоступна; Exact Cache допускает работу без кеша."""


class CachedAnswer(RagResult):
    """Ответ Exact Cache с поколением корпуса для проверки актуальности."""
    corpus_generation: NonNegativeInt = Field(description='Поколение набора доступных редакций; 0 допустим для пустого корпуса.')


class CacheRead(DTO):
    """Прочитанный ответ и токен, защищающий условное удаление от гонки с записью."""
    value: CachedAnswer = Field(description='Ответ и поколение, прочитанные из Redis.')
    compare_token: bytes = Field(description='Непрозрачный токен прочитанного значения для атомарного условного удаления.')


async def read_cached_answer(query: str) -> CacheRead | None:
    """Прочитать точный кеш и токен его наблюдаемого значения.

    Args:
        query: Нормализованный вопрос из Guardrails.

    Returns:
        Объект Pydantic `CacheRead` или `None`.
    """
    _text(query)
    state = _get()
    key = state.settings.cache_prefix + hashlib.sha256(query.encode("utf-8")).hexdigest()
    try:
        async with asyncio.timeout(state.settings.io_timeout_s):
            value, token = await state.redis.eval(
                "return {redis.call('GET',KEYS[1]) or false,redis.call('GET',KEYS[2]) or false}",2,key,key+":token")
        if value is None or not isinstance(token,bytes) or len(token)!=16:
            return None
        try:
            cached = CachedAnswer.model_validate_json(value)
        except (ValidationError,ValueError,TypeError):
            return None
        return CacheRead(value=cached,compare_token=token)
    except (RedisError,OSError,TimeoutError) as exc:
        raise CacheUnavailable("Redis read failed") from exc


async def delete_cached_answer_if_unchanged(query: str, compare_token: bytes) -> bool:
    """Атомарно удалить только ранее прочитанное значение кеша.

    Args:
        query: Нормализованный вопрос из Guardrails.
        compare_token: Токен из CacheRead, идентифицирующий прочитанное значение.

    Returns:
        Одно логическое значение.
    """
    _text(query)
    state = _get()
    key = state.settings.cache_prefix + hashlib.sha256(query.encode("utf-8")).hexdigest()
    try:
        async with asyncio.timeout(state.settings.io_timeout_s):
            return bool(await state.redis.eval("""if redis.call('GET',KEYS[2])==ARGV[1] then
                local found=redis.call('EXISTS',KEYS[1]);redis.call('DEL',KEYS[1],KEYS[2]);return found
                else return 0 end""",2,key,key+":token",compare_token))
    except (RedisError,OSError,TimeoutError) as exc:
        raise CacheUnavailable("Redis conditional delete failed") from exc


async def write_cached_answer_if_not_older(query: str, value: CachedAnswer) -> bool:
    """Атомарно записать ответ с TTL без перезаписи более нового поколения.

    Args:
        query: Нормализованный вопрос из Guardrails.
        value: Сохранённый результат и поколение, по которому он получен.

    Returns:
        Одно логическое значение.
    """
    _text(query)
    value = CachedAnswer.model_validate_json(value.model_dump_json())
    if not value.sources:
        raise ValueError("Only answers with sources may enter Exact Cache")
    state = _get()
    key = state.settings.cache_prefix + hashlib.sha256(query.encode("utf-8")).hexdigest()
    try:
        async with asyncio.timeout(state.settings.io_timeout_s):
            while True:
                async with state.redis.pipeline(transaction=True) as pipe:
                    try:
                        await pipe.watch(key,key+":token")
                        old = await pipe.get(key)
                        if old is not None:
                            try:
                                current = CachedAnswer.model_validate_json(old)
                            except (ValidationError,ValueError,TypeError):
                                current = None
                            if current is not None and current.corpus_generation>value.corpus_generation:
                                return False
                        pipe.multi()
                        pipe.set(key,value.model_dump_json(),ex=state.settings.cache_ttl_s)
                        pipe.set(key+":token",uuid4().bytes,ex=state.settings.cache_ttl_s)
                        await pipe.execute()
                        return True
                    except WatchError:
                        continue
    except (RedisError,OSError,TimeoutError) as exc:
        raise CacheUnavailable("Redis conditional write failed") from exc
