"""Independent Runtime process used to verify durable inference deduplication."""

import asyncio
import json
import sys

from rag.model_gateway.entrypoints import start_models, close_models, embed_texts
from rag.model_gateway.providers import configure_coordination
from rag.runtime._model_calls import _PostgresModelCalls
from rag.runtime.events import _event_context
from rag.runtime.lifecycle import start_runtime, close_runtime


async def _main():
    await start_runtime()
    configure_coordination(_PostgresModelCalls())
    try:
        await start_models(json.loads(sys.argv[1]))
        with _event_context(request_id=123, attempt_no=int(sys.argv[2])):
            result = await embed_texts(["DELAY"], "query", timeout_s=10)
        print("RESULT " + result.model_dump_json(), flush=True)
    finally:
        await close_models()
        configure_coordination(None)
        await close_runtime()


if __name__ == "__main__":
    asyncio.run(_main())
