"""Coordinate Gateway and orphan sweeps through process-owned filesystem locks."""

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from rag.contracts import StorageError
from rag.runtime import _spool
from rag.runtime._state import _get
from rag.runtime.events import emit_event


@asynccontextmanager
async def _lease(ref):
    guard = _spool._guard(ref, upload_lease=True)
    acquiring = asyncio.create_task(asyncio.to_thread(_spool._call, guard.__enter__))
    try:
        target = await asyncio.shield(acquiring)
    except asyncio.CancelledError:
        # to_thread cannot be cancelled once it has started. Never abandon an
        # acquired file handle, or a sweep could wait until process termination.
        await acquiring
        await asyncio.to_thread(guard.__exit__, None, None, None)
        raise
    try:
        yield target is not None
    finally:
        await asyncio.shield(asyncio.to_thread(guard.__exit__, None, None, None))


@asynccontextmanager
async def _protect_upload(revision_id):
    async with _lease(f'uploads/{revision_id}') as acquired:
        if not acquired:
            raise StorageError('Upload is already active')
        yield


async def _sweep_orphans(limit):
    from rag.runtime.postgresql.cleanup import is_staging_referenced
    from rag.runtime.upload_spool import list_staging_candidates, remove_staged_upload

    cutoff = datetime.now(timezone.utc) - timedelta(seconds=_get().settings.recovery_window_s)
    cursor, removed = None, 0
    while True:
        candidates = await list_staging_candidates(cutoff, limit, cursor)
        if not candidates:
            return removed
        for ref in candidates:
            async with _lease(ref) as acquired:
                if acquired and not await is_staging_referenced(ref):
                    # The upload lease prevents a later registration from
                    # starting between this committed DB check and deletion.
                    await remove_staged_upload(ref)
                    removed += 1
        cursor = candidates[-1]


async def _sweep_loop(stop):
    while not stop.is_set():
        try:
            await asyncio.wait_for(stop.wait(), _get().settings.recovery_interval_s)
        except TimeoutError:
            try:
                await _sweep_orphans(_get().settings.batch_size)
            except Exception:
                emit_event('runtime', 'upload_orphan_cleanup', 'finished', str(uuid4()),
                           outcome='retry_pending')
