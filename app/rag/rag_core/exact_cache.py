"""Contracts: docs/documents/rag-core/exact-cache-entrypoints.md."""
import asyncio
from contextlib import contextmanager
from uuid import uuid4
from rag.contracts import RagResult
from rag.runtime import redis
from rag.runtime.events import emit_event, _parent_step, _span
from ._settings import _Settings

def _validate(query, corpus_generation):
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be nonempty")
    if type(corpus_generation) is not int or corpus_generation < 0:
        raise ValueError("Invalid corpus generation")

@contextmanager
def _cache_span(name, corpus_generation):
    record = _parent_step.get()
    if record is not None and record["name"] == name:
        record["corpus_generation"] = corpus_generation
        yield record
    else:
        with _span(name, corpus_generation=corpus_generation) as record:
            yield record

def _unavailable(record, operation, exc):
    code = "cache_timeout" if isinstance(exc, TimeoutError) else "cache_unavailable"
    record.update(outcome="unavailable", error_code=code)
    emit_event("exact_cache", operation, "finished", str(uuid4()),
               outcome="unavailable", error_code=code)

async def get_answer(query: str, corpus_generation: int) -> RagResult | None:
    """Read an answer of the pinned generation; bound Redis failures."""
    _validate(query, corpus_generation)
    with _cache_span("get_answer", corpus_generation) as record:
        record["outcome"] = "cache_miss"
        try:
            async with asyncio.timeout(_Settings().cache_timeout_s):
                with _span("read_cached_answer") as read:
                    entry = await redis.read_cached_answer(query)
                    read["outcome"] = "found" if entry is not None else "not_found"
                if entry is None:
                    record["cache_reason"] = "not_found"
                    return None
                record["cached_generation"] = entry.value.corpus_generation
                if entry.value.corpus_generation < corpus_generation:
                    record["cache_reason"] = "older_generation"
                    with _span("delete_cached_answer_if_unchanged") as delete:
                        deleted = await redis.delete_cached_answer_if_unchanged(query, entry.compare_token)
                        delete["outcome"] = "deleted" if deleted else "not_deleted"
                elif entry.value.corpus_generation > corpus_generation:
                    record["cache_reason"] = "newer_generation"
                elif entry.value.sources:
                    result = RagResult.model_validate(entry.value.model_dump(exclude={"corpus_generation"}))
                    record.update(outcome="cache_hit", cache_reason="matching_generation")
                    return result
                else:
                    record["cache_reason"] = "missing_sources"
        except (redis.CacheUnavailable, TimeoutError) as exc:
            _unavailable(record, "get_answer", exc)
    return None

async def store_answer(query: str, corpus_generation: int, result: RagResult) -> None:
    """Best effort conditional write of a committed answer with sources."""
    _validate(query, corpus_generation)
    result = RagResult.model_validate(result)
    value = redis.CachedAnswer(**result.model_dump(), corpus_generation=corpus_generation)
    if not value.sources:
        raise ValueError("Only answers with sources may enter Exact Cache")
    with _cache_span("store_answer", corpus_generation) as record:
        try:
            async with asyncio.timeout(_Settings().cache_timeout_s):
                with _span("write_cached_answer_if_not_older") as write:
                    stored = await redis.write_cached_answer_if_not_older(query, value)
                    write["outcome"] = "stored" if stored else "skipped_newer_generation"
                record["outcome"] = write["outcome"]
        except (redis.CacheUnavailable, TimeoutError) as exc:
            _unavailable(record, "store_answer", exc)
