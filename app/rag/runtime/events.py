"""Structured events for docs/documents/faq/logging.md."""

import json
from typing import Literal


def emit_event(
    component: str,
    operation: str,
    event: Literal["started", "finished"],
    run_id: str,
    **fields: object,
) -> None:
    """Вывести одно JSON-событие в stdout и сразу сбросить буфер.

    Args:
        component: Владелец операции, например rag_core.
        operation: Имя операции пайплайна.
        event: started или finished.
        run_id: ID запуска операции для связи событий.
        **fields: Поля диагностики по спецификации логов; без токенов и секретов.

    Returns:
        None. Поля должны быть JSON-сериализуемыми; схему функция не валидирует.
    """
    record = {
        "schema_version": 1,
        "component": component,
        "operation": operation,
        "event": event,
        "run_id": run_id,
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)
