"""Contracts: docs/documents/rag-core/entrypoints.md."""


from rag.contracts import ConsumeDecision, RequestRef


async def handle_rag_question(event: RequestRef) -> ConsumeDecision:
    raise NotImplementedError
