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
    record = {
        "schema_version": 1,
        "component": component,
        "operation": operation,
        "event": event,
        "run_id": run_id,
        **fields,
    }
    print(json.dumps(record, ensure_ascii=False), flush=True)
