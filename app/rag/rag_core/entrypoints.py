"""Contracts: docs/documents/rag-core/entrypoints.md."""


from rag.contracts import ConsumeDecision, RequestRef


async def handle_rag_question(event: RequestRef) -> ConsumeDecision:
    """Оркестрировать Guardrails → Exact Cache → Searcher → Answer Generator.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        event: Ссылка на строку запроса в PostgreSQL.

    Returns:
        ConsumeDecision для адаптера RabbitMQ. Ответ сохраняется с outbox rag.results
        транзакцией complete_question; непосредственно в очередь его отправляет Relay.
    """
    raise NotImplementedError
