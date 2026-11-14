"""Contracts: docs/documents/model-gateway/providers.md.

Provider extension points; no SDK, model or process is initialized on import.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
import re
from threading import RLock

from rag.contracts import StepError


class ModelAdapter(ABC):
    """Async provider lifecycle. CPU inference belongs in a managed executor."""

    execution = None

    @abstractmethod
    async def start(self):
        """Open resources; partial startup must be safe to close."""

    @abstractmethod
    async def invoke(self, operation, payload, *, timeout_s):
        """Return a result DTO/dict, or GenerationStream for generate."""

    async def probe(self, operation, payload, *, timeout_s):
        return [await self.invoke(operation, payload, timeout_s=timeout_s)]

    @abstractmethod
    async def close(self):
        """Stop actual executors and clients; repeated calls must be safe."""


class LocalModel(ABC):
    """Synchronous backend instantiated only inside the local worker process."""

    @abstractmethod
    def invoke(self, operation, payload):
        """Run the selected model and return its normalized DTO/dict."""

    def close(self):
        """Release backend resources before the worker exits."""


class GenerationStream:
    """Normalized stream: text deltas followed by exactly one terminal event."""

    def __init__(self, profile_id, chunks):
        self.profile_id = profile_id
        self.chunks = chunks


class CallCoordinator(ABC):
    """Runtime-owned coordination; the Gateway never accesses a database."""

    def namespace(self):
        return ""

    @abstractmethod
    async def acquire(self, key, profile_id, role, operation):
        """Return owned(token), done(value), busy or uncertain."""

    @abstractmethod
    async def complete(self, key, token, value):
        """Fence the owner and durably save the result before returning it."""

    @abstractmethod
    async def release(self, key, token, *, safe):
        """Release only unstarted work; unknown inference outcomes stay blocked."""


_factories = {}
_registry_lock = RLock()
_frozen = False
_coordinator = None


def register_provider(name: str, factory: Callable) -> None:
    """Register a side-effect-free factory(binding, role) -> ModelAdapter."""
    if (
        not isinstance(name, str)
        or not re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", name)
        or name == "local_process"
        or not callable(factory)
    ):
        raise StepError("model_configuration_error", False)
    with _registry_lock:
        if _frozen or (name in _factories and _factories[name] is not factory):
            raise StepError("model_configuration_error", False)
        _factories[name] = factory


def unregister_provider(name: str) -> None:
    """Remove a provider before startup or after successful shutdown."""
    if not isinstance(name, str):
        raise StepError("model_configuration_error", False)
    with _registry_lock:
        if _frozen:
            raise StepError("model_configuration_error", False)
        _factories.pop(name, None)


def configure_coordination(coordinator: object | None) -> None:
    """Install a Runtime coordinator before startup; None selects process memory."""
    global _coordinator
    if coordinator is not None and not isinstance(coordinator, CallCoordinator):
        raise StepError("model_configuration_error", False)
    with _registry_lock:
        if _frozen:
            raise StepError("model_configuration_error", False)
        _coordinator = coordinator


def _freeze(value):
    global _frozen
    with _registry_lock:
        _frozen = value


def _snapshot():
    with _registry_lock:
        return dict(_factories)


def _restore(snapshot):
    with _registry_lock:
        _factories.clear()
        _factories.update(snapshot)
