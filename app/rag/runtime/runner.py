"""Contracts: docs/documents/infrastructure/runtime-entrypoints.md.

Composition of infrastructure and owner-supplied business handlers.
"""

import ast
import asyncio
from contextlib import asynccontextmanager
import inspect
import os
import signal
from typing import Literal
from uuid import uuid4

from pydantic import TypeAdapter
from sqlalchemy import func, select

from rag.contracts import StepError
from rag.model_gateway.contracts import ModelBinding, ModelRole
from rag.runtime._state import _get
from rag.runtime.events import emit_event
from rag.runtime.lifecycle import start_runtime, close_runtime, prepare_runtime_storage
from rag.runtime.postgresql import cleanup, outbox
from rag.runtime.queue_relay import relay_outbox_batch
from rag.runtime.rabbitmq import _consume


def _implemented(function):
    """Reject the repository's explicit placeholders before consuming saved work."""
    return not any(
        isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Name)
        and node.exc.id == "NotImplementedError"
        for node in ast.walk(ast.parse(inspect.getsource(function)))
    )


def _bindings(component, settings):
    bindings = TypeAdapter(dict[ModelRole, ModelBinding]).validate_json(
        settings.model_bindings.get_secret_value()
    )
    required = (
        {"embedding", "generation", "injection", "ocr"}
        if component == "questions"
        else {"embedding", "generation"} if component == "ingestion" else set()
    )
    if component == "questions" and settings.use_reranker:
        required.add("reranker")
    if not required <= bindings.keys():
        raise StepError("model_bindings_missing", False)
    selected = {role: bindings[role] for role in required}
    embedding = selected.get("embedding")
    if embedding is not None and (
        embedding.profile_id != settings.embedding_profile
        or embedding.embedding_dimensions != settings.embedding_dimensions
    ):
        raise StepError("embedding_profile_mismatch", False)
    return selected


async def _cleanup_batch(limit):
    from rag.runtime.qdrant import delete_revision_points
    from rag.runtime.upload_spool import remove_staged_upload

    for item in await cleanup.list_finished_staging(limit):
        await remove_staged_upload(item.staging_ref)
        await cleanup.clear_staging_ref(item.revision_id, item.staging_ref)
    for revision in await cleanup.claim_revision_cleanup(limit):
        await delete_revision_points(revision)
        await cleanup.finish_revision_cleanup(revision)


async def _recover_batch(limit):
    for route in ("rag.questions", "rag.results", "ingestion.jobs"):
        await outbox.recover_pending_work(route, limit)
    await _cleanup_batch(limit)


@asynccontextmanager
async def _singleton(component):
    # A dedicated live connection owns the session lock, never a pooled transaction.
    engine = _get().engine
    async with engine.connect() as connection:
        acquired = await connection.scalar(
            select(func.pg_try_advisory_lock(728342, func.hashtext(component)))
        )
        await connection.commit()
        if not acquired:
            raise RuntimeError("A background process for this role is already active")
        try:
            yield connection
        finally:
            await connection.execute(
                select(func.pg_advisory_unlock(728342, func.hashtext(component)))
            )
            await connection.commit()


async def _periodic(component, operation, interval, stop):
    async with _singleton(component) as ownership:
        while not stop.is_set():
            # Detect loss of the singleton lock's connection; never silently reacquire.
            await ownership.execute(select(1))
            await ownership.commit()
            try:
                await operation(_get().settings.batch_size)
            except Exception:
                emit_event(
                    "runtime",
                    component,
                    "finished",
                    str(uuid4()),
                    outcome="retry_pending",
                )
            try:
                await asyncio.wait_for(stop.wait(), timeout=interval)
            except TimeoutError:
                pass


async def run_runtime(
    component: Literal[
        "relay", "recovery", "questions", "results", "maintenance", "ingestion"
    ],
) -> None:
    """Run one separately supervised role; SIGTERM drains work and closes resources."""
    if component not in (
        "relay",
        "recovery",
        "questions",
        "results",
        "maintenance",
        "ingestion",
    ):
        raise ValueError("Unknown runtime component")
    from rag.ingestion.worker import handle_ingestion_job
    from rag.rag_core.entrypoints import handle_rag_question
    from rag.rag_api.entrypoints import handle_rag_result, run_api_maintenance
    from rag.model_gateway.entrypoints import start_models, close_models

    handlers = {
        "questions": handle_rag_question,
        "results": handle_rag_result,
        "ingestion": handle_ingestion_job,
        "maintenance": run_api_maintenance,
    }
    handler = handlers.get(component)
    if handler is not None and not _implemented(handler):
        raise StepError("business_handler_not_implemented", False)
    if component == "ingestion" and "RAG_WATCHDOG_FD" not in os.environ:
        raise StepError("worker_supervisor_required", False)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for signum in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(signum, stop.set)
            installed.append(signum)
        except (NotImplementedError, RuntimeError):
            pass
    models_started = False
    coordination_set = False
    try:
        await start_runtime()
        settings = _get().settings
        bindings = _bindings(component, settings)
        if bindings:
            from rag.model_gateway.providers import configure_coordination
            from rag.runtime._model_calls import _PostgresModelCalls

            configure_coordination(_PostgresModelCalls())
            coordination_set = True
            await prepare_runtime_storage()
            models_started = True  # Close partial startup too.
            async with asyncio.timeout(settings.startup_timeout_s):
                await start_models(bindings)
        if component == "relay":
            await _periodic(
                component, relay_outbox_batch, settings.relay_interval_s, stop
            )
        elif component == "recovery":
            await _periodic(
                component, _recover_batch, settings.recovery_interval_s, stop
            )
        elif component == "maintenance":
            await _periodic(component, handler, settings.recovery_interval_s, stop)
        else:
            route = {
                "questions": "rag.questions",
                "results": "rag.results",
                "ingestion": "ingestion.jobs",
            }[component]
            if component == "ingestion":
                await _recover_batch(settings.batch_size)
                async with _singleton(component):
                    await _consume(route, handler, stop)
            else:
                await _consume(route, handler, stop)
    finally:
        try:
            if models_started:
                async with asyncio.timeout(_get().settings.shutdown_timeout_s):
                    await close_models()
        finally:
            if coordination_set:
                try:
                    configure_coordination(None)
                except StepError:
                    emit_event(
                        "runtime",
                        "close_models",
                        "finished",
                        str(uuid4()),
                        outcome="shutdown_incomplete",
                    )
            await close_runtime()
            for signum in installed:
                loop.remove_signal_handler(signum)
