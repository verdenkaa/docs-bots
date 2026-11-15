"""Real filesystem leases: cancellation, process death and fail-closed DB checks."""

import asyncio
import os
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from rag.contracts import StorageError
from rag.ingestion.contracts import ConvertedDocument, UploadSource
from rag.runtime import _state, upload_spool
from rag.runtime._uploads import _protect_upload, _sweep_orphans


class UploadCleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        settings = SimpleNamespace(spool_dir=Path(self.temporary.name), io_timeout_s=2,
                                   max_markdown_bytes=10000, recovery_window_s=1)
        self.enterContext(patch.object(_state, '_current', SimpleNamespace(settings=settings)))
        self.referenced = self.enterContext(patch('rag.runtime.postgresql.cleanup.is_staging_referenced',
                                                AsyncMock(return_value=False)))
        self.revision = uuid4()
        self.ref = await upload_spool.stage_upload(self.revision, ConvertedDocument(markdown='Document'),
            UploadSource(filename='Guide', access_level='employees'))
        self.path = settings.spool_dir / self.ref
        os.utime(self.path, (1,1))

    async def test_active_upload_and_cancellation_release_lease(self):
        entered = asyncio.Event()
        async def upload():
            async with _protect_upload(self.revision):
                entered.set()
                await asyncio.Event().wait()
        task = asyncio.create_task(upload())
        try:
            await asyncio.wait_for(entered.wait(), 2)
            self.assertEqual(await _sweep_orphans(1), 0)
            self.referenced.assert_not_awaited()
        finally:
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(await _sweep_orphans(1), 1)
        self.assertFalse(self.path.exists())

    async def test_database_outage_preserves_files_and_registered_upload_is_skipped(self):
        self.referenced.side_effect = StorageError('unavailable')
        with self.assertRaises(StorageError):
            await _sweep_orphans(1)
        self.assertTrue(self.path.exists())
        self.referenced.side_effect = None
        self.referenced.return_value = True
        self.assertEqual(await _sweep_orphans(1), 0)
        self.assertTrue(self.path.exists())

    async def test_process_crash_releases_os_lease_for_recovery(self):
        script = '''import sys
from pathlib import Path
from types import SimpleNamespace
from rag.runtime import _state, _spool
_state._set(SimpleNamespace(settings=SimpleNamespace(spool_dir=Path(sys.argv[1]),io_timeout_s=2)))
with _spool._guard(sys.argv[2],upload_lease=True) as target:
    print('locked',flush=True)
    sys.stdin.readline()
'''
        process = await asyncio.create_subprocess_exec(sys.executable, '-c', script,
            self.temporary.name, self.ref, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            ready = await asyncio.wait_for(process.stdout.readline(), 15)
            self.assertEqual(ready.strip(), b'locked')
            self.assertEqual(await _sweep_orphans(1), 0)
            self.assertTrue(self.path.exists())
        finally:
            if process.returncode is None:
                process.kill()
            await asyncio.wait_for(process.communicate(), 5)
        self.assertEqual(await _sweep_orphans(1), 1)
        self.assertFalse(self.path.exists())
