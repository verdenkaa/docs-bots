"""Contracts: docs/documents/guardrails/entrypoints.md."""


from rag.contracts import GuardrailsResult


async def check_question(raw_question: str) -> GuardrailsResult:
    """Проверить и нормализовать вопрос до обращения к кешу и поиску.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        raw_question: Исходный текст из PostgreSQL для проверки и нормализации. Пустая строка или только пробелы дают invalid_input.

    Returns:
        GuardrailsResult: разрешённый текст, отказ либо недоступность детектора.
        Решение по score принимает Guardrails; документы в MVP здесь не проверяются.
    """
    raise NotImplementedError
