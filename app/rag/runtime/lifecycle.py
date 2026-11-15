"""Contracts: docs/documents/infrastructure/runtime-entrypoints.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""

import asyncio
from pathlib import Path

import httpx
from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from qdrant_client import AsyncQdrantClient
from redis.asyncio import Redis
from sqlalchemy import func, select
from sqlalchemy.schema import CreateSchema
from sqlalchemy.ext.asyncio import create_async_engine

from rag.runtime import _state
from rag.runtime._config import RuntimeSettings
from rag.runtime.postgresql._models import _CorpusState, _SchemaVersion


def _engine(settings):
    return create_async_engine(
        settings._database_url(),
        pool_size=settings.pg_pool_size,
        max_overflow=0,
        pool_pre_ping=True,
        hide_parameters=True,
        connect_args={
            "command_timeout": settings.io_timeout_s,
            "server_settings": {
                "search_path": settings.pg_schema,
                "lock_timeout": f"{int(settings.io_timeout_s * 1000)}ms",
            },
        },
    )


def _migration_config():
    config = Config()
    config.set_main_option("script_location", str(Path(__file__).parent / "_migrations"))
    return config


async def migrate_runtime() -> None:
    """Apply versioned migrations once, outside application startup."""
    settings = RuntimeSettings()
    engine = _engine(settings)
    try:
        async with engine.begin() as connection:
            await connection.execute(select(func.pg_advisory_xact_lock(728341, 1)))
            await connection.execute(CreateSchema(settings.pg_schema, if_not_exists=True))

            def upgrade(sync_connection):
                config = _migration_config()
                config.attributes.update(connection=sync_connection, schema=settings.pg_schema)
                command.upgrade(config, "head")

            await connection.run_sync(upgrade)
    finally:
        await engine.dispose()


async def start_runtime() -> None:
    """Create per-process clients and verify the PostgreSQL schema.

    Redis is optional for answering. Models and business processes are started by
    the composition runner, not by importing or creating infrastructure clients.
    """
    async with _state._lifecycle_lock:
        if _state._current is not None:
            return
        settings = RuntimeSettings()
        resources = _state._Resources(settings)
        try:
            resources.engine = _engine(settings)
            async with resources.engine.connect() as connection:
                version = await connection.scalar(select(_SchemaVersion.version_num))
                if version != ScriptDirectory.from_config(_migration_config()).get_current_head():
                    raise RuntimeError(
                        "Runtime schema requires migration; run python -m rag.runtime migrate"
                    )
                await connection.execute(
                    select(_CorpusState.generation).where(_CorpusState.singleton_id == 1)
                )
            resources.redis = Redis.from_url(
                settings.redis_url.get_secret_value(),
                socket_timeout=settings.io_timeout_s,
                socket_connect_timeout=settings.io_timeout_s,
            )
            resources.http = httpx.AsyncClient(
                timeout=settings.io_timeout_s, follow_redirects=False, trust_env=False
            )
            resources.qdrant = AsyncQdrantClient(
                url=settings.qdrant_url,
                api_key=(
                    settings.qdrant_api_key.get_secret_value() if settings.qdrant_api_key else None
                ),
                timeout=int(settings.io_timeout_s),
            )
        except BaseException:
            await _close(resources)
            raise
        _state._set(resources)


async def close_runtime() -> None:
    """Close all clients even on a partial startup; safe to call repeatedly."""
    async with _state._lifecycle_lock:
        resources = _state._current
        _state._set(None)
        if resources is not None:
            await _close(resources)


async def _close(resources):
    failures = []
    from rag.runtime._photos import _close_photos

    try:
        async with asyncio.timeout(resources.settings.shutdown_timeout_s):
            await _close_photos(resources)
    except Exception as exc:
        failures.append(exc)
    for resource, method in (
        (resources.channel, "close"),
        (resources.broker, "close"),
        (resources.qdrant, "close"),
        (resources.http, "aclose"),
        (resources.redis, "aclose"),
        (resources.engine, "dispose"),
    ):
        if resource is not None:
            try:
                async with asyncio.timeout(resources.settings.shutdown_timeout_s):
                    await getattr(resource, method)()
            except Exception as exc:
                failures.append(exc)
    if failures:
        raise RuntimeError("Runtime resource shutdown failed") from failures[0]


async def prepare_runtime_storage() -> None:
    """Initialize a new corpus or validate both existing indexes and the S3 bucket."""
    from rag.runtime._qdrant import _prepare
    from rag.runtime._http import _ensure_bucket
    from rag.runtime.postgresql.ingestion import list_required_revision_manifests
    from rag.runtime.qdrant import verify_revision_points, verify_topic_points
    from rag.contracts import StepError

    await _prepare()
    for manifest in await list_required_revision_manifests():
        if (
            manifest.expected_chunk_count is None
            or manifest.expected_topic_count is None
            or not await verify_revision_points(manifest.revision_id, manifest.expected_chunk_count)
            or not await verify_topic_points(manifest.revision_id, manifest.expected_topic_count)
        ):
            raise StepError("qdrant_recovery_required", False)
    await _ensure_bucket()
