"""Contracts: docs/documents/rag-core/exact-cache-entrypoints.md."""


from rag.contracts import RagResult


async def get_answer(query: str, corpus_generation: int) -> RagResult | None:
    raise NotImplementedError


async def store_answer(query: str, corpus_generation: int, result: RagResult) -> None:
    raise NotImplementedError
