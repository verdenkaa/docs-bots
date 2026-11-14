"""Private bindings to the existing Runtime and Model Gateway operations."""

from datetime import UTC, datetime
from time import monotonic

from rag.model_gateway import entrypoints as models
from rag.runtime import events, qdrant, upload_spool
from rag.runtime.postgresql import cleanup, ingestion


class _Dependencies:
    """Importing adapters does not create clients or start services.

    Tests replace these module-shaped ports on an instance. The production
    bindings deliberately call the existing adapters, including their stubs.
    """

    def __init__(self) -> None:
        self.jobs = ingestion
        self.spool = upload_spool
        self.index = qdrant
        self.models = models
        self.cleanup = cleanup
        self.emit = events.emit_event

    @staticmethod
    def now() -> datetime:
        return datetime.now(UTC)

    @staticmethod
    def monotonic() -> float:
        return monotonic()
