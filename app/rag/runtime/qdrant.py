"""Contracts: docs/documents/infrastructure/qdrant/contracts.md."""


from collections.abc import Sequence
from typing import TypedDict
from uuid import UUID

from rag.contracts import SearchFragment


class ChunkPoint(SearchFragment):
    chunk_no: int
    dense: list[float]


class QdrantHit(TypedDict):
    fragment: SearchFragment
    score: float


async def embed_texts(texts: Sequence[str]) -> list[list[float]]:
    raise NotImplementedError


async def find_chunks(
    query: str,
    query_vector: Sequence[float],
    revision_ids: frozenset[UUID],
    candidate_limit: int,
) -> list[QdrantHit]:
    raise NotImplementedError


async def write_revision_points(revision_id: UUID, points: Sequence[ChunkPoint]) -> None:
    raise NotImplementedError


async def verify_revision_points(revision_id: UUID, expected_chunk_count: int) -> bool:
    raise NotImplementedError


async def delete_revision_points(revision_id: UUID) -> None:
    raise NotImplementedError
