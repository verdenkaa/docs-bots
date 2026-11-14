"""Failure boundaries independent of external services and model providers."""

import base64
import json
from pathlib import Path
import sys
import tempfile
import threading
from time import monotonic, sleep
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from rag.contracts import StepError
from rag.runtime import _state
from rag.runtime._config import RuntimeSettings
from rag.runtime._supervisor import _supervise, _group_alive
from rag.runtime.lifecycle import close_runtime
from rag.runtime.media import get_question_image
from rag.runtime.runner import _bindings, run_runtime


PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")


class RuntimeFaultTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.state = _state._Resources(RuntimeSettings(telegram_bot_token="test-token"))
        _state._set(self.state)

    async def asyncTearDown(self):
        await close_runtime()

    async def client(self, handler):
        self.state.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def test_telegram_rejects_path_escape_and_classifies_transient_status(self):
        for path in ("../secret", "/absolute", "https://example.test/a", "x\\a", "x?a"):
            with self.subTest(path=path):
                await self.client(lambda request: httpx.Response(200, json={"ok": True, "result": {"file_path": path}}))
                with self.assertRaises(StepError) as raised:
                    await get_question_image("file-id")
                self.assertFalse(raised.exception.retryable)
                await self.state.http.aclose()
        await self.client(lambda request: httpx.Response(429))
        with self.assertRaises(StepError) as raised:
            await get_question_image("file-id")
        self.assertTrue(raised.exception.retryable)

    async def test_telegram_download_checks_bytes_and_size(self):
        def handler(request):
            if request.method == "POST":
                return httpx.Response(200, json={"ok": True, "result": {"file_path": "photos/image.png"}})
            return httpx.Response(200, content=PNG, headers={"content-type": "text/plain"})
        await self.client(handler)
        image = await get_question_image("file-id")
        self.assertEqual((image.data, image.mime_type), (PNG, "image/png"))
        self.state.settings.max_image_bytes = 10
        with self.assertRaisesRegex(StepError, "image_too_large"):
            await get_question_image("file-id")

    async def test_shutdown_closes_every_resource_despite_one_failure(self):
        broken = AsyncMock()
        broken.close.side_effect = RuntimeError("Injected")
        http, redis, engine = AsyncMock(), AsyncMock(), AsyncMock()
        self.state.channel, self.state.http = broken, http
        self.state.redis, self.state.engine = redis, engine
        with self.assertRaises(RuntimeError):
            await close_runtime()
        http.aclose.assert_awaited_once()
        redis.aclose.assert_awaited_once()
        engine.dispose.assert_awaited_once()
        await close_runtime()
        with self.assertRaises(RuntimeError):
            _state._get()

    async def test_roles_refuse_placeholder_handlers_before_resources(self):
        with patch("rag.runtime.runner.start_runtime", new=AsyncMock()) as start:
            with self.assertRaisesRegex(StepError, "business_handler_not_implemented"):
                await run_runtime("questions")
            start.assert_not_awaited()

    def test_bindings_require_roles_and_profile_and_do_not_load_unused_roles(self):
        self.assertEqual(_bindings("relay", self.state.settings), {})
        with self.assertRaisesRegex(StepError, "model_bindings_missing"):
            _bindings("questions", self.state.settings)
        binding = dict(profile_id="profile", execution="remote", provider="test", model_id="fixed",
            model_revision="1", endpoint="https://example.test", credential_env=None, max_concurrency=1,
            embedding_dimensions=3)
        from pydantic import SecretStr
        self.state.settings.model_bindings = SecretStr(json.dumps({role: binding for role in ("embedding", "generation", "ocr")}))
        with self.assertRaisesRegex(StepError, "embedding_profile_mismatch"):
            _bindings("ingestion", self.state.settings)
        self.state.settings.embedding_profile = "profile"
        self.state.settings.embedding_dimensions = 3
        self.assertEqual(set(_bindings("ingestion", self.state.settings)), {"embedding", "generation"})


@unittest.skipUnless(sys.platform == "linux", "Worker supervision runs inside a Linux container")
class WorkerSupervisorTests(unittest.TestCase):
    def test_hung_process_and_descendant_are_dead_before_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "child.py"
            script.write_text('''import os, signal, subprocess, sys, time
from pathlib import Path
root=Path(sys.argv[1])
if not (root/'first').exists():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child=subprocess.Popen([sys.executable,'-c','import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(30)'])
    (root/'first').write_text(str(os.getpid())+','+str(child.pid))
    os.write(int(os.environ['RAG_WATCHDOG_FD']),b'{"budget":0.3,"step":"injected_hang"}\\n')
    time.sleep(30)
else:
    (root/'restarted').write_text('yes')
    os.write(int(os.environ['RAG_WATCHDOG_FD']),b'{"budget":null,"step":"idle"}\\n')
    time.sleep(30)
''', encoding="utf-8")
            stop = threading.Event()

            def monitor():
                end = monotonic() + 10
                while not (root / "restarted").exists() and monotonic() < end:
                    sleep(0.02)
                stop.set()

            thread = threading.Thread(target=monitor)
            thread.start()
            try:
                _supervise([sys.executable, str(script), directory], stop,
                    startup_timeout=2, shutdown_timeout=0.5, restart_delay=0.01)
            finally:
                stop.set()
                thread.join(timeout=1)
            self.assertTrue((root / "restarted").exists())
            group = int((root / "first").read_text().split(",")[0])
            self.assertFalse(_group_alive(group))
