"""Real PostgreSQL and independent local executors; no model SDK or weights."""

import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from sqlalchemy import text

from rag.contracts import StepError
from rag.runtime import _state
from rag.runtime._model_calls import _PostgresModelCalls, _pending, _reset
from rag.runtime.events import _event_context
from rag.runtime.lifecycle import migrate_runtime, start_runtime, close_runtime


@unittest.skipUnless(
    os.environ.get("RUN_RUNTIME_INTEGRATION") == "1", "Use tests/runtime-compose.yaml"
)
class DurableModelCallsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "runtime_" + uuid4().hex
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "RAG_PG_SCHEMA": self.schema,
                "MODEL_GATEWAY_CACHE_DIR": self.temp.name,
                "MODEL_GATEWAY_PROVIDER_MODULES": "[]",
                "MODEL_GATEWAY_LOCAL_LOADERS": json.dumps(
                    {"durable-fixture": "tests.model_gateway_fixtures:create_model"}
                ),
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        await migrate_runtime()
        await start_runtime()

    async def asyncTearDown(self):
        try:
            async with _state._get().engine.begin() as connection:
                assert self.schema.startswith("runtime_") and len(self.schema) == 40
                await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        finally:
            await close_runtime()

    async def test_atomic_owner_fencing_replay_and_unknown_outcome(self):
        coordinator = _PostgresModelCalls()
        key = "a" * 64
        records = await asyncio.gather(
            *(
                coordinator.acquire(key, "profile", "embedding", "embed_texts")
                for _ in range(12)
            )
        )
        owned = [record for record in records if record["kind"] == "owned"]
        self.assertEqual(len(owned), 1)
        self.assertEqual(sum(record["kind"] == "busy" for record in records), 11)
        token = owned[0]["token"]
        with self.assertRaises(StepError):
            await coordinator.complete(key, str(uuid4()), {"result": {}})
        await coordinator.release(key, token, safe=False)
        self.assertEqual(
            (
                await _PostgresModelCalls().acquire(
                    key, "profile", "embedding", "embed_texts"
                )
            )["kind"],
            "uncertain",
        )
        with self.assertRaises(ValueError):
            await _reset(key, False)
        self.assertEqual((await _pending(1))[0]["state"], "uncertain")
        self.assertTrue(await _reset(key, True))
        replacement = await coordinator.acquire(
            key, "profile", "embedding", "embed_texts"
        )
        value = {"result": {"profile_id": "profile", "vectors": [[1.0]]}}
        await coordinator.complete(key, replacement["token"], value)
        await coordinator.release(key, token, safe=True)
        self.assertEqual(
            await _PostgresModelCalls().acquire(
                key, "profile", "embedding", "embed_texts"
            ),
            {"kind": "done", "value": value},
        )
        self.assertFalse(await _reset(key, True))
        self.assertEqual(await _pending(1), [])

    async def test_namespaces_ignore_attempt_but_separate_requests_and_jobs(self):
        coordinator = _PostgresModelCalls()
        with _event_context(request_id=4, attempt_no=1):
            first = coordinator.namespace()
        with _event_context(request_id=4, attempt_no=2):
            self.assertEqual(coordinator.namespace(), first)
        with _event_context(request_id=5):
            self.assertNotEqual(coordinator.namespace(), first)
        with _event_context(job_id=4):
            self.assertNotEqual(coordinator.namespace(), first)

    async def test_two_processes_and_restart_execute_question_only_once(self):
        bindings = json.dumps(
            {
                "embedding": dict(
                    profile_id="durable-fixture",
                    execution="local",
                    provider="local_process",
                    model_id="fixture",
                    model_revision="1",
                    endpoint=None,
                    credential_env=None,
                    max_concurrency=1,
                    embedding_dimensions=3,
                )
            }
        )

        async def run(attempt):
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                "-m",
                "tests.model_gateway_process",
                bindings,
                str(attempt),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            try:
                output, errors = await asyncio.wait_for(process.communicate(), 30)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            self.assertEqual(process.returncode, 0, errors.decode())
            result = next(
                line.removeprefix("RESULT ")
                for line in output.decode().splitlines()
                if line.startswith("RESULT ")
            )
            return json.loads(result)

        results = await asyncio.gather(run(1), run(2))
        results.append(await run(3))
        self.assertTrue(all(result["vectors"] == [[5.0] * 3] for result in results))
        records = [
            json.loads(line)
            for file in Path(self.temp.name).rglob("fixture-calls.jsonl")
            for line in file.read_text().splitlines()
        ]
        self.assertEqual(sum(record.get("marker") == "DELAY" for record in records), 1)
        self.assertEqual(sum(record["operation"] == "load" for record in records), 3)
        self.assertEqual(await _pending(10), [])
