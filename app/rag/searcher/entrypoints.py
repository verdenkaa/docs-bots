"""Contracts: docs/documents/retrieval/entrypoints.md."""


from uuid import UUID

from rag.contracts import SearchFragment


async def search(query: str, revision_ids: frozenset[UUID]) -> list[SearchFragment]:
    raise NotImplementedError
