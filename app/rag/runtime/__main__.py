"""Explicit operational commands, separate from the application scaffold."""

import argparse
import asyncio
import json
import signal
import sys
import threading

from rag.runtime._config import RuntimeSettings
from rag.runtime.lifecycle import (
    migrate_runtime,
    start_runtime,
    close_runtime,
    prepare_runtime_storage,
)
from rag.runtime.runner import run_runtime


async def _command(args):
    if args.command == "check-models":
        from pydantic import TypeAdapter
        from rag.model_gateway.contracts import ModelRole, ModelBinding
        from rag.model_gateway.entrypoints import start_models, close_models
        from rag.contracts import StepError

        settings = RuntimeSettings()
        try:
            bindings = TypeAdapter(dict[ModelRole, ModelBinding]).validate_json(
                settings.model_bindings.get_secret_value()
            )
            if args.role:
                bindings = {role: bindings[role] for role in args.role}
        except (ValueError, KeyError):
            raise StepError("model_configuration_error", False) from None
        try:
            await start_models(bindings)
            print(
                json.dumps(
                    {role: binding.profile_id for role, binding in bindings.items()}
                )
            )
        finally:
            await close_models()
        return
    if args.command == "migrate":
        await migrate_runtime()
        return
    if args.command == "serve":
        await run_runtime(args.component)
        return
    await start_runtime()
    try:
        if args.command == "prepare":
            await prepare_runtime_storage()
        elif args.command == "pending-writes":
            from rag.runtime.postgresql.ingestion import list_pending_revision_writes

            for item in await list_pending_revision_writes(args.limit):
                print(item.model_dump_json())
        elif args.command == "set-access":
            from rag.rag_api.contracts import AccessRecord, Principal
            from rag.runtime._state import _get
            from rag.runtime.postgresql.support import write_user_access

            settings = _get().settings
            record = await write_user_access(
                Principal(login=settings.admin_login.get_secret_value()),
                AccessRecord(
                    username=args.username.removeprefix("@").lower(),
                    enabled=not args.disable,
                ),
            )
            print(record.model_dump_json())
        elif args.command == "model-calls":
            from rag.runtime._model_calls import _pending

            for row in await _pending(args.limit):
                print(json.dumps(row))
        elif args.command == "reset-model-call":
            from rag.runtime._model_calls import _reset

            print(json.dumps({"reset": await _reset(args.key, args.writers_stopped)}))
    finally:
        await close_runtime()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "prepare"):
        commands.add_parser(name)
    pending = commands.add_parser("pending-writes")
    pending.add_argument("--limit", type=int, default=100)
    access = commands.add_parser("set-access")
    access.add_argument("username")
    access.add_argument("--disable", action="store_true")
    model_calls = commands.add_parser("model-calls")
    model_calls.add_argument("--limit", type=int, default=100)
    reset = commands.add_parser("reset-model-call")
    reset.add_argument("key")
    reset.add_argument("--writers-stopped", action="store_true", required=True)
    check = commands.add_parser("check-models")
    check.add_argument(
        "--role",
        action="append",
        choices=("embedding", "reranker", "injection", "generation", "ocr", "vision"),
    )
    serve = commands.add_parser("serve")
    serve.add_argument(
        "component",
        choices=(
            "relay",
            "recovery",
            "questions",
            "results",
            "maintenance",
            "ingestion",
        ),
    )
    commands.add_parser("worker")
    args = parser.parse_args()
    if args.command == "worker":
        from rag.runtime._supervisor import _supervise
        from rag.runtime.runner import _implemented, _bindings
        from rag.ingestion.worker import handle_ingestion_job
        from rag.contracts import StepError

        settings = RuntimeSettings()
        if not _implemented(handle_ingestion_job):
            raise StepError("business_handler_not_implemented", False)
        _bindings("ingestion", settings)
        stop = threading.Event()
        for signum in (signal.SIGTERM, signal.SIGINT):
            signal.signal(signum, lambda *_: stop.set())
        _supervise(
            [sys.executable, "-m", "rag.runtime", "serve", "ingestion"],
            stop,
            startup_timeout=settings.startup_timeout_s,
            shutdown_timeout=settings.shutdown_timeout_s,
        )
    else:
        asyncio.run(_command(args))


if __name__ == "__main__":
    main()
