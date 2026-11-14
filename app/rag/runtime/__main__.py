"""Explicit operational commands, separate from the application scaffold."""

import argparse
import asyncio
import signal
import sys
import threading

from rag.runtime._config import RuntimeSettings
from rag.runtime.lifecycle import migrate_runtime, start_runtime, close_runtime, prepare_runtime_storage
from rag.runtime.runner import run_runtime


async def _command(args):
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
        elif args.command == "bootstrap-admin":
            from rag.runtime.postgresql._db import _transaction, _lock, _one, _execute
            async with _transaction() as connection:
                await _lock(connection, "bootstrap-admin", 0)
                if await _one(connection, "SELECT user_id FROM employees WHERE role='admin' LIMIT 1"):
                    raise RuntimeError("An administrator already exists; use Access operations")
                await _execute(connection, "INSERT INTO employees(user_id,enabled,role) VALUES(:id,true,'admin')",
                               id=args.user_id)
    finally:
        await close_runtime()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("migrate", "prepare"):
        commands.add_parser(name)
    pending = commands.add_parser("pending-writes")
    pending.add_argument("--limit", type=int, default=100)
    admin = commands.add_parser("bootstrap-admin")
    admin.add_argument("user_id", type=int)
    serve = commands.add_parser("serve")
    serve.add_argument("component", choices=("relay", "recovery", "questions", "results", "maintenance", "ingestion"))
    commands.add_parser("worker")
    args = parser.parse_args()
    if args.command == "bootstrap-admin" and args.user_id <= 0:
        parser.error("user_id must be positive")
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
        _supervise([sys.executable, "-m", "rag.runtime", "serve", "ingestion"], stop,
                   startup_timeout=settings.startup_timeout_s, shutdown_timeout=settings.shutdown_timeout_s)
    else:
        asyncio.run(_command(args))


if __name__ == "__main__":
    main()
