"""Contracts: docs/documents/guardrails/entrypoints.md."""


from rag.contracts import GuardrailsResult


async def check_question(raw_question: str) -> GuardrailsResult:
    raise NotImplementedError
