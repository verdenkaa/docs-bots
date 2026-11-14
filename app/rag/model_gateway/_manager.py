"""Per-process lifecycle and bounded admission, independent of business modules."""

import asyncio
import base64
import hashlib
import importlib
import inspect
import os
from time import monotonic
from uuid import uuid4

from rag.contracts import StepError
from rag.model_gateway import providers
from rag.model_gateway._config import _Settings
from rag.model_gateway._streams import _collect
from rag.model_gateway._validation import _bindings, _error, _input, _result, _timeout
from rag.model_gateway.providers import GenerationStream, ModelAdapter
from rag.model_gateway._coordination import _MemoryCoordinator
from rag.model_gateway._wire import _encode
from rag.runtime.events import emit_event, _event_context
from rag.runtime._watchdog import _step


class _Role:
    def __init__(self, binding, adapter):
        self.binding, self.adapter = binding, adapter
        self.semaphore = asyncio.Semaphore(binding.max_concurrency)
        self.jobs = set()
        self.flights = {}


class _Manager:
    def __init__(self, bindings, settings):
        self.bindings, self.settings = bindings, settings
        self.roles = {}
        self.ready = False
        self.closers = {}
        self.coordinator = providers._coordinator or _MemoryCoordinator(settings)


class _Control:
    def __init__(self, loop):
        self.loop, self.lock, self.manager = loop, asyncio.Lock(), None


_control = None


def _get_control():
    global _control
    loop = asyncio.get_running_loop()
    if _control is None or (_control.loop.is_closed() and _control.manager is None):
        _control = _Control(loop)
    if _control.loop is not loop:
        raise StepError("model_configuration_error", False)
    return _control


def _probe(role):
    if role == "embedding":
        return "embed_texts", dict(texts=["Model readiness probe"], purpose="query")
    if role == "reranker":
        return "rerank", dict(query="probe", candidates=["probe"])
    if role == "injection":
        return "classify_injection", dict(text="How do I open the documentation?")
    if role == "generation":
        return "generate", dict(
            messages=[dict(role="user", content="Reply with OK.")],
            options=dict(max_output_tokens=8, temperature=None),
        )
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII="
    )
    return ("recognize_text" if role == "ocr" else "describe_image"), dict(
        image=dict(data=image, mime_type="image/png")
    )


async def _shutdown(manager):
    manager.ready = False
    settings = manager.settings
    jobs = {job for state in manager.roles.values() for job in state.jobs}
    for job in jobs:
        if not job.cancelling():
            job.cancel()
    for role, state in manager.roles.items():
        task = manager.closers.get(role)
        if task is None or (
            task.done() and (task.cancelled() or task.exception() is not None)
        ):
            manager.closers[role] = asyncio.create_task(state.adapter.close())
    tasks = jobs | set(manager.closers.values())
    if tasks:
        _, pending = await asyncio.wait(tasks, timeout=settings.shutdown_timeout_s)
        if pending:
            # Keep a closing manager and the frozen registry. Starting a second
            # copy while an old executor still owns resources is forbidden.
            raise StepError("model_timeout", True)
        if any(
            task.cancelled() or task.exception() is not None
            for task in manager.closers.values()
        ):
            raise StepError("model_unavailable", True)


async def _start(value):
    bindings = _bindings(value)
    control = _get_control()
    try:
        settings = _Settings()
    except ValueError:
        raise StepError("model_configuration_error", False) from None
    async with control.lock:
        if control.manager is not None:
            if (
                control.manager.ready
                and control.manager.bindings == bindings
                and all(
                    getattr(state.adapter, "ready", True)
                    for state in control.manager.roles.values()
                )
            ):
                return
            raise StepError("model_configuration_error", False)
        snapshot = providers._snapshot()
        manager = _Manager(bindings, settings)
        control.manager = manager
        run_id, started = str(uuid4()), monotonic()
        outcome, code = "ready", None
        with _event_context():
            emit_event("model_gateway", "start_models", "started", run_id)
        try:
            async with asyncio.timeout(settings.startup_timeout_s):
                for module_name in settings.provider_modules if bindings else ():
                    try:
                        module = await asyncio.to_thread(
                            importlib.import_module, module_name
                        )
                        hook = getattr(module, "register_providers", None)
                        if not callable(hook) or inspect.iscoroutinefunction(hook):
                            raise ValueError()
                        hook()
                    except Exception:
                        raise StepError("model_configuration_error", False) from None
                providers._freeze(True)
                factories = providers._snapshot()
                for role, binding in bindings.items():
                    if binding.credential_env is not None and not os.environ.get(
                        binding.credential_env
                    ):
                        raise StepError("model_configuration_error", False)
                    if binding.provider == "local_process":
                        from rag.model_gateway._local import _LocalAdapter

                        adapter = _LocalAdapter(binding, role, settings)
                    else:
                        factory = factories.get(binding.provider)
                        if factory is None:
                            raise StepError("model_configuration_error", False)
                        try:
                            adapter = factory(binding.model_copy(deep=True), role)
                        except Exception:
                            raise StepError(
                                "model_configuration_error", False
                            ) from None
                    if (
                        not isinstance(adapter, ModelAdapter)
                        or adapter.execution != binding.execution
                    ):
                        raise StepError("model_configuration_error", False)
                    manager.roles[role] = _Role(binding, adapter)
                    await adapter.start()
                    operation, payload = _probe(role)
                    results = await adapter.probe(
                        operation, payload, timeout_s=settings.startup_timeout_s
                    )
                    if not results:
                        raise StepError("model_invalid_response", False)
                    for result in results:
                        if isinstance(result, GenerationStream):
                            if role != "generation":
                                raise StepError("model_invalid_response", False)
                            result = await _collect(result, settings.max_message_bytes)
                        _result(
                            role, binding, payload, result, settings.max_message_bytes
                        )
                manager.ready = True
        except BaseException as exc:
            outcome, code = "failed", (
                "cancelled"
                if isinstance(exc, asyncio.CancelledError)
                else _error(exc).code
            )
            cleanup = asyncio.create_task(_shutdown(manager))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                # An independent shutdown task still owns every created adapter.
                cleanup.add_done_callback(_consume_exception)
                raise
            except Exception:
                raise StepError("model_unavailable", True) from None
            else:
                control.manager = None
                providers._freeze(False)
                providers._restore(snapshot)
            if isinstance(exc, asyncio.CancelledError):
                raise
            raise _error(exc) from None
        finally:
            with _event_context():
                emit_event(
                    "model_gateway",
                    "start_models",
                    "finished",
                    run_id,
                    outcome=outcome,
                    error_code=code,
                    duration_ms=round((monotonic() - started) * 1000),
                )


async def _close():
    control = _get_control()
    async with control.lock:
        manager = control.manager
        if manager is None:
            return
        run_id, started = str(uuid4()), monotonic()
        with _event_context():
            emit_event("model_gateway", "close_models", "started", run_id)
        code = None
        try:
            task = asyncio.create_task(_shutdown(manager))
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                task.add_done_callback(_consume_exception)
                raise
            control.manager = None
            providers._freeze(False)
        except StepError as exc:
            code = exc.code
            raise
        finally:
            with _event_context():
                emit_event(
                    "model_gateway",
                    "close_models",
                    "finished",
                    run_id,
                    outcome="closed" if code is None else "failed",
                    error_code=code,
                    duration_ms=round((monotonic() - started) * 1000),
                )


def _consume_exception(task):
    if not task.cancelled():
        task.exception()


async def _job(state, role, operation, payload, deadline, manager, timing, key):
    settings, coordinator = manager.settings, manager.coordinator
    async with state.semaphore:
        token, invoked, finishing = None, False, False
        try:
            while True:
                record = await coordinator.acquire(
                    key, state.binding.profile_id, role, operation
                )
                if record["kind"] == "done":
                    value = record["value"]
                    if "error" in value:
                        raise StepError(value["error"], False)
                    timing["reused"] = True
                    return _result(
                        role,
                        state.binding,
                        payload,
                        value["result"],
                        settings.max_message_bytes,
                    )
                if record["kind"] == "owned":
                    token = record["token"]
                    break
                if record["kind"] == "uncertain":
                    raise StepError("model_unavailable", True)
                if deadline <= monotonic():
                    raise StepError("model_timeout", True)
                await asyncio.sleep(
                    min(settings.coordination_poll_s, max(0, deadline - monotonic()))
                )
            timing["inference_started"] = monotonic()
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise StepError("model_timeout", True)
            invoked = True
            result = await state.adapter.invoke(operation, payload, timeout_s=remaining)
            if isinstance(result, GenerationStream):
                if operation != "generate":
                    raise StepError("model_invalid_response", False)
                result = await _collect(result, settings.max_message_bytes)
            result = _result(
                role, state.binding, payload, result, settings.max_message_bytes
            )
            finishing = True
            commit = asyncio.create_task(
                coordinator.complete(
                    key, token, {"result": result.model_dump(mode="json")}
                )
            )
            try:
                await asyncio.shield(commit)
            except asyncio.CancelledError:
                # A lost response must not discard an already computed result.
                await commit
                raise
            return result
        except BaseException as exc:
            if token is not None and not finishing:
                error = _error(exc)
                if isinstance(exc, Exception) and not error.retryable:
                    await coordinator.complete(key, token, {"error": error.code})
                else:
                    await coordinator.release(key, token, safe=not invoked)
            raise


class _Flight:
    def __init__(self, job, timing):
        self.job, self.timing, self.waiters = job, timing, 0


def _finished(state, key, flight, job):
    state.jobs.discard(job)
    if state.flights.get(key) is flight:
        del state.flights[key]
    _consume_exception(job)


async def _call(role, operation, raw, timeout_s):
    started, run_id = monotonic(), str(uuid4())
    timing, job, state, flight, attached = {}, None, None, None, False
    outcome, code, usage, joined = "failed", None, None, False
    emit_event("model_gateway", operation, "started", run_id, model_role=role)
    try:
        timeout = _timeout(timeout_s)
        control = _get_control()
        manager = control.manager
        if manager is None or not manager.ready or role not in manager.roles:
            raise StepError("model_not_ready", False)
        state, settings = manager.roles[role], manager.settings
        if not getattr(state.adapter, "ready", True):
            raise StepError("model_not_ready", False)
        payload = _input(operation, raw, settings)
        if operation == "embed_texts" and not payload["texts"]:
            result = _result(
                role,
                state.binding,
                payload,
                dict(profile_id=state.binding.profile_id, vectors=[]),
                settings.max_message_bytes,
            )
        elif operation == "rerank" and not payload["candidates"]:
            result = _result(
                role,
                state.binding,
                payload,
                dict(profile_id=state.binding.profile_id, scores=[]),
                settings.max_message_bytes,
            )
        else:
            deadline = started + timeout
            if deadline <= monotonic():
                raise StepError("model_timeout", True)
            try:
                digest = hashlib.sha256(
                    _encode(
                        dict(
                            binding=state.binding.model_dump(),
                            operation=operation,
                            payload=payload,
                        ),
                        settings.max_message_bytes,
                    )
                ).hexdigest()
            except (ValueError, TypeError):
                raise StepError("model_invalid_input", False) from None
            key = hashlib.sha256(
                (manager.coordinator.namespace() + "\0" + digest).encode()
            ).hexdigest()
            flight = state.flights.get(key)
            if flight is None:
                if (
                    len(state.jobs)
                    >= state.binding.max_concurrency + settings.queue_size
                ):
                    raise StepError("model_overloaded", True)
                job = asyncio.create_task(
                    _job(
                        state, role, operation, payload, deadline, manager, timing, key
                    )
                )
                flight = _Flight(job, timing)
                state.flights[key] = flight
                state.jobs.add(job)
                job.add_done_callback(lambda task: _finished(state, key, flight, task))
            else:
                timing, job = flight.timing, flight.job
                if job.cancelling():
                    raise StepError("model_overloaded", True)
                joined = True
            flight.waiters += 1
            attached = True
            with _step(timeout, "model:" + role):
                result = await asyncio.wait_for(
                    asyncio.shield(job), timeout=max(0, deadline - monotonic())
                )
        outcome = "completed"
        usage = getattr(result, "usage", None)
        return result.model_copy(deep=True)
    except asyncio.CancelledError:
        code, outcome = "cancelled", "cancelled"
        raise
    except Exception as exc:
        error = _error(exc)
        code = error.code
        raise error from None
    finally:
        end = monotonic()
        if attached:
            flight.waiters -= 1
            if flight.waiters == 0 and not flight.job.done():
                flight.job.cancel()
        inference = max(started, timing.get("inference_started", end))
        fields = dict(
            model_role=role,
            outcome=outcome,
            error_code=code,
            wait_ms=round((inference - started) * 1000),
            inference_ms=round((end - inference) * 1000),
            duration_ms=round((end - started) * 1000),
        )
        fields["reused"] = joined or timing.get("reused", False)
        if state is not None:
            fields.update(
                profile_id=state.binding.profile_id, execution=state.binding.execution
            )
        if usage is not None:
            fields.update(usage.model_dump())
        emit_event("model_gateway", operation, "finished", run_id, **fields)
