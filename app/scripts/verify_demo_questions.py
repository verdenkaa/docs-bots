"""Inspect ingestion or exercise the real RAG pipeline without sending Telegram messages.

Run inside rag-live-questions: python scripts/verify_demo_questions.py --status
Then omit --status and optionally supply one or more question strings.
"""

import argparse
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from uuid import uuid4

from sqlalchemy import select

from rag.contracts import ClaimedQuestion, ProcessingAttempt, TextQuestion
from rag.model_gateway.entrypoints import start_models, close_models
from rag.rag_core.entrypoints import _pipeline
from rag.rag_core._settings import _Settings
from rag.runtime._state import _get
from rag.runtime.lifecycle import start_runtime, close_runtime
from rag.runtime.postgresql._models import _Revision, _CorpusState, _ModelCall
from rag.runtime.runner import _bindings


async def run(args):
    await start_runtime()
    models_started = False
    try:
        async with _get().engine.connect() as connection:
            rows = (await connection.execute(select(
                _Revision.source_name, _Revision.state, _Revision.expected_chunk_count,
                _Revision.expected_topic_count,
            ))).all()
            generation = await connection.scalar(select(_CorpusState.generation))
            if args.model_status:
                results = (await connection.execute(select(_ModelCall.result).where(
                    _ModelCall.model_role == "generation", _ModelCall.state == "completed",
                ).limit(10))).scalars().all()
                for result in results:
                    text = result.get("result", result).get("text", "")
                    try:
                        value = json.loads(text)
                        summary = {"type": type(value).__name__, "keys": list(value) if isinstance(value, dict) else None,
                                   "question_count": len(value.get("questions", [])) if isinstance(value, dict) else None}
                    except ValueError:
                        summary = {"invalid_json_prefix": text[:100]}
                    print(json.dumps(summary, ensure_ascii=False), flush=True)
        print(json.dumps({"documents": [list(row) for row in rows], "generation": generation},
                         ensure_ascii=False), flush=True)
        if args.status or args.model_status:
            return
        if len([row for row in rows if row.state == "published"]) < 3:
            raise RuntimeError("Wait for all three documents to be published")
        models_started = True
        bindings = _bindings("questions", _get().settings)
        if args.local:
            bindings = {role: binding.model_copy(update={"endpoint": "http://127.0.0.1:11434"})
                        if binding.provider == "ollama" else binding
                        for role, binding in bindings.items()}
        await start_models(bindings)
        for text in args.questions or (
            "Какой статус по умолчанию у мебели при добавлении в реестр мебели?",
            "Сколько записей можно создать за одно массовое добавление в реестре мебели?",
        ):
            now = datetime.now(timezone.utc)
            # Diagnostic input only: no fabricated Telegram update or stored request.
            question = ClaimedQuestion(
                attempt=ProcessingAttempt(request_id=1, attempt_no=1,
                                          deadline_at=now + timedelta(seconds=300)),
                input=TextQuestion(kind="text", text=text), received_at=now,
                corpus_generation=generation,
            )
            outcome, normalized = await _pipeline(question, _Settings())
            print(json.dumps({"question": text, "outcome": outcome.model_dump(mode="json")},
                             ensure_ascii=False), flush=True)
    finally:
        if models_started:
            await close_models()
        await close_runtime()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--model-status", action="store_true")
    parser.add_argument("--local", action="store_true", help="Use published localhost development ports")
    parser.add_argument("questions", nargs="*")
    args = parser.parse_args()
    if args.local:
        os.environ.update(PGHOST="127.0.0.1", PGPORT="5432", QDRANT_URL="http://127.0.0.1:6333",
                          REDIS_URL="redis://127.0.0.1:6379/0")
    asyncio.run(run(args))
