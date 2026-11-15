"""Bounded cleanup of work explicitly authorized by PostgreSQL."""

from __future__ import annotations

import asyncio
from uuid import UUID
from rag.runtime.events import _span

from rag.contracts import StepError
from rag.ingestion._dependencies import _Dependencies
from rag.ingestion._execution import _Boundary, _Budget
from rag.ingestion._settings import _Settings
from rag.runtime.postgresql.cleanup import StagingCleanup


class _Maintenance:
    def __init__(self, settings: _Settings, deps: _Dependencies) -> None:
        self.settings = settings
        self.deps = deps
        self.budget = _Budget.for_seconds(settings.cleanup_timeout_s, deps)

    async def _call(self, name, function, annotation, *args):
        with _span(name):
            async with asyncio.timeout(self.budget.remaining(self.settings.step_timeout_s)):
                raw = await function(*args)
            return _Boundary.validate(raw, annotation, "cleanup_invalid_response")

    async def _staging(self) -> None:
        rows = await self._call("list_finished_staging", self.deps.cleanup.list_finished_staging,
                                list[StagingCleanup], self.settings.cleanup_limit)
        for row in rows:
            await self._call("remove_staged_upload", self.deps.spool.remove_staged_upload,
                             type(None), row.staging_ref)
            await self._call("clear_staging_ref", self.deps.cleanup.clear_staging_ref,
                             bool, row.revision_id, row.staging_ref)

    async def _revisions(self) -> None:
        revisions = await self._call("claim_revision_cleanup", self.deps.cleanup.claim_revision_cleanup,
                                     list[UUID], self.settings.cleanup_limit)
        for revision_id in revisions:
            # This adapter deletes and verifies both chunk and topic points.
            await self._call("delete_revision_points", self.deps.index.delete_revision_points,
                             type(None), revision_id)
            await self._call("finish_revision_cleanup", self.deps.cleanup.finish_revision_cleanup,
                             bool, revision_id)

    async def run(self) -> list[str]:
        errors: list[str] = []
        # After publication/failure only: PostgreSQL authorizes each deletion.
        # remove → clear_staging; claim_cleanup → delete both indexes → finish_cleanup.
        for operation in (self._staging, self._revisions):
            try:
                await operation()
            except Exception as error:
                # Cleanup cannot undo the confirmed outcome of the main job.
                # Cancellation is BaseException and deliberately propagates.
                errors.append(error.code if isinstance(error, StepError)
                              else "cleanup_" + type(error).__name__)
        return errors
