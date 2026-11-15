"""Contracts: docs/documents/rag-core/exact-cache-entrypoints.md."""
import asyncio
from rag.contracts import RagResult
from rag.runtime import redis
from rag.runtime.events import emit_event, _parent_step
from ._settings import _Settings

def _validate(query, corpus_generation):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be nonempty")
    if type(corpus_generation) is not int or corpus_generation < 0:
        raise ValueError("Invalid corpus generation")

async def get_answer(query: str, corpus_generation: int) -> RagResult | None:
    """Read an answer of the pinned generation; bound Redis failures."""
    _validate(query, corpus_generation)
    try:
        async with asyncio.timeout(_Settings().cache_timeout_s):
            entry = await redis.read_cached_answer(query)
            if entry is None:
                return None
            if entry.value.corpus_generation < corpus_generation:
                await redis.delete_cached_answer_if_unchanged(query, entry.compare_token)
            elif entry.value.corpus_generation == corpus_generation and entry.value.sources:
                return RagResult.model_validate(entry.value.model_dump(exclude={"corpus_generation"}))
    except (redis.CacheUnavailable, TimeoutError):
        record = _parent_step.get()
        if record is not None and record["name"] == "get_answer":
            record["outcome"] = "unavailable"
        emit_event("exact_cache", "get_answer", "finished", "cache", outcome="unavailable")
    return None

async def store_answer(query: str, corpus_generation: int, result: RagResult) -> None:
    """Best effort conditional write of a committed answer with sources."""
    _validate(query, corpus_generation)
    result = RagResult.model_validate(result)
    value = redis.CachedAnswer(**result.model_dump(), corpus_generation=corpus_generation)
    if not value.sources:
        raise ValueError("Only answers with sources may enter Exact Cache")
    try:
        async with asyncio.timeout(_Settings().cache_timeout_s):
            await redis.write_cached_answer_if_not_older(query, value)
    except (redis.CacheUnavailable, TimeoutError):
        record = _parent_step.get()
        if record is not None and record["name"] == "store_answer":
            record["outcome"] = "unavailable"
        emit_event("exact_cache", "store_answer", "finished", "cache", outcome="unavailable")
