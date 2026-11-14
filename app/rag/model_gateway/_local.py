"""Built-in local process transport. The selected loader supplies the model."""

import asyncio
import hashlib
import os
import re
import subprocess
import sys

from rag.contracts import StepError
from rag.model_gateway.providers import ModelAdapter
from rag.model_gateway._processes import _terminate, _tree
from rag.model_gateway._validation import _result, _temporary
from rag.model_gateway._wire import _decode, _encode


class _WireError(Exception):
    def __init__(self, code):
        self.code = code


class _Worker:
    def __init__(self, binding, role, settings, loader):
        self.binding, self.role, self.settings, self.loader = (
            binding,
            role,
            settings,
            loader,
        )
        self.process = None
        self.loaded = self.busy = False
        self.lock = asyncio.Lock()
        self.descendants = {}

    async def _receive(self):
        try:
            header = await self.process.stdout.readexactly(4)
            size = int.from_bytes(header, "big")
            if size > self.settings.max_message_bytes:
                raise _WireError("model_invalid_response")
            response = _decode(await self.process.stdout.readexactly(size))
            if not isinstance(response, dict):
                raise ValueError()
            if "error" in response:
                code = response["error"]["code"]
                if not isinstance(code, str):
                    raise ValueError()
                raise StepError(code, code in _temporary)
            return response
        except (asyncio.IncompleteReadError, BrokenPipeError, ConnectionError):
            raise _WireError("model_unavailable") from None
        except (ValueError, TypeError, KeyError):
            raise _WireError("model_invalid_response") from None

    async def _exchange(self, value, timeout):
        try:
            encoded = _encode(value, self.settings.max_message_bytes)
        except (ValueError, TypeError):
            raise StepError("model_invalid_input", False) from None
        async with asyncio.timeout(timeout):
            self.process.stdin.write(encoded)
            await self.process.stdin.drain()
            return await self._receive()

    async def start(self):
        allowed = {
            "PATH",
            "SYSTEMROOT",
            "WINDIR",
            "TEMP",
            "TMP",
            "LANG",
            "PYTHONPATH",
            "LD_LIBRARY_PATH",
            "CUDA_VISIBLE_DEVICES",
            "HF_HOME",
            "TORCH_HOME",
            "TRANSFORMERS_CACHE",
        }
        environment = {
            name: value for name, value in os.environ.items() if name.upper() in allowed
        }
        if self.binding.credential_env is not None:
            environment[self.binding.credential_env] = os.environ[
                self.binding.credential_env
            ]
        options = (
            {"creationflags": subprocess.CREATE_NO_WINDOW}
            if sys.platform == "win32"
            else {}
        )
        task = asyncio.create_task(
            asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "rag.model_gateway._local_worker",
                str(self.settings.max_message_bytes),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=environment,
                **options
            )
        )
        try:
            self.process = await asyncio.shield(task)
        except BaseException:
            try:
                self.process = await task
            except Exception:
                pass
            else:
                await self.stop()
            raise
        cache = (
            self.settings.cache_dir
            / hashlib.sha256(self.binding.profile_id.encode()).hexdigest()
        )
        if self.process.returncode is None:
            self.descendants.update(
                await asyncio.to_thread(
                    _tree, self.process.pid, self.descendants.get(self.process.pid)
                )
            )
        await asyncio.to_thread(cache.mkdir, parents=True, exist_ok=True)
        response = await self._exchange(
            dict(
                binding=self.binding.model_dump(),
                role=self.role,
                loader=self.loader,
                cache_dir=str(cache),
                memory_bytes=self.settings.local_memory_bytes,
            ),
            self.settings.startup_timeout_s,
        )
        if response != {"ok": True}:
            raise _WireError("model_invalid_response")
        self.loaded = True

    async def call(self, operation, payload, timeout):
        self.busy = True
        try:
            response = await self._exchange(
                dict(kind="invoke", operation=operation, payload=payload), timeout
            )
            if set(response) != {"result"}:
                raise _WireError("model_invalid_response")
            return response["result"]
        finally:
            self.busy = False

    async def stop(self):
        async with self.lock:
            if self.process is not None:
                await _terminate(self.process, self.descendants)
                self.loaded = False

    async def close(self):
        if self.process is None:
            return
        if self.process.returncode is None:
            self.descendants.update(
                await asyncio.to_thread(
                    _tree, self.process.pid, self.descendants.get(self.process.pid)
                )
            )
        try:
            if self.loaded and not self.busy and self.process.returncode is None:
                await self._exchange({"kind": "close"}, self.settings.stop_grace_s)
                await asyncio.wait_for(
                    self.process.wait(), timeout=self.settings.stop_grace_s
                )
        except (Exception, asyncio.CancelledError):
            pass
        finally:
            await self.stop()


class _LocalAdapter(ModelAdapter):
    execution = "local"

    def __init__(self, binding, role, settings):
        self.binding, self.role, self.settings = binding, role, settings
        self.loader = settings.local_loaders.get(binding.profile_id, "")
        if (
            binding.execution != "local"
            or not re.fullmatch(
                r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", self.loader
            )
            or binding.max_concurrency > settings.max_local_workers
        ):
            raise StepError("model_configuration_error", False)
        self.workers = set()
        self.available = asyncio.Queue()
        self.closed = False
        self.ready = False
        self.probe_call = None
        self.repairs = set()

    async def start(self):
        for _ in range(self.binding.max_concurrency):
            worker = _Worker(self.binding, self.role, self.settings, self.loader)
            self.workers.add(worker)
            await worker.start()
            self.available.put_nowait(worker)
        self.ready = True

    async def probe(self, operation, payload, *, timeout_s):
        self.probe_call = operation, payload
        return await asyncio.gather(
            *(worker.call(operation, payload, timeout_s) for worker in self.workers)
        )

    async def _replace(self, worker):
        await worker.stop()
        self.workers.discard(worker)
        if self.closed:
            return
        replacement = _Worker(self.binding, self.role, self.settings, self.loader)
        self.workers.add(replacement)
        try:
            async with asyncio.timeout(self.settings.startup_timeout_s):
                await replacement.start()
                if self.probe_call is not None:
                    operation, payload = self.probe_call
                    result = await replacement.call(
                        operation, payload, self.settings.startup_timeout_s
                    )
                    _result(
                        self.role,
                        self.binding,
                        payload,
                        result,
                        self.settings.max_message_bytes,
                    )
            if not self.closed:
                self.available.put_nowait(replacement)
            else:
                await replacement.stop()
        except BaseException:
            self.ready = False
            await replacement.stop()
            raise

    async def invoke(self, operation, payload, *, timeout_s):
        if self.closed or not self.ready:
            raise StepError("model_not_ready", False)
        worker = await self.available.get()
        healthy = True
        try:
            return await worker.call(operation, payload, timeout_s)
        except (asyncio.CancelledError, TimeoutError, _WireError, OSError) as exc:
            healthy = False
            # Replacement is lifecycle work, never a retry of the failed input.
            task = asyncio.create_task(self._replace(worker))
            self.repairs.add(task)
            task.add_done_callback(self.repairs.discard)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                task.add_done_callback(
                    lambda job: None if job.cancelled() else job.exception()
                )
                raise
            if isinstance(exc, asyncio.CancelledError):
                raise
            if isinstance(exc, TimeoutError):
                raise StepError("model_timeout", True) from None
            if isinstance(exc, _WireError):
                raise StepError(exc.code, exc.code in _temporary) from None
            raise StepError("model_unavailable", True) from None
        finally:
            if healthy and not self.closed:
                self.available.put_nowait(worker)

    async def close(self):
        self.closed, self.ready = True, False
        repairs = tuple(self.repairs)
        for task in repairs:
            if not task.cancelling():
                task.cancel()
        if repairs:
            await asyncio.gather(*repairs, return_exceptions=True)
        await asyncio.gather(*(worker.close() for worker in tuple(self.workers)))
