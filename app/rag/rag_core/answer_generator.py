"""Contracts: docs/documents/rag-core/answer-generator-entrypoints.md."""


from collections.abc import Sequence

from rag.contracts import GenerationResult, SearchFragment


async def generate_answer(query: str, fragments: Sequence[SearchFragment]) -> GenerationResult:
    raise NotImplementedError
