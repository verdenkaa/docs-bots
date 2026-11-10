"""Contracts: docs/documents/infrastructure/redis/contracts.md."""


from typing import TypedDict

from rag.contracts import RagResult


class CachedAnswer(RagResult):
    corpus_generation: int


class CacheRead(TypedDict):
    value: CachedAnswer
    compare_token: bytes


async def read_cached_answer(query: str) -> CacheRead | None:
    raise NotImplementedError


async def delete_cached_answer_if_unchanged(query: str, compare_token: bytes) -> bool:
    raise NotImplementedError


async def write_cached_answer_if_not_older(query: str, value: CachedAnswer) -> bool:
    raise NotImplementedError
