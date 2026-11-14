"""Real process execution and termination using a backend with no model weights."""

import asyncio
from contextlib import redirect_stdout
import hashlib
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

from rag.contracts import StepError, ImageContent
from rag.model_gateway import _manager
from rag.model_gateway.contracts import ModelBinding, ModelMessage, GenerationOptions
from rag.model_gateway.entrypoints import (
    start_models,
    close_models,
    embed_texts,
    generate,
    rerank,
    classify_injection,
    recognize_text,
    describe_image,
)
from rag.model_gateway._processes import _info


class LocalModelGatewayTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.env = patch.dict(
            os.environ,
            {
                "MODEL_GATEWAY_CACHE_DIR": self.temp.name,
                "MODEL_GATEWAY_STARTUP_TIMEOUT_S": "15",
                "MODEL_GATEWAY_SHUTDOWN_TIMEOUT_S": "4",
                "MODEL_GATEWAY_STOP_GRACE_S": "0.1",
                "MODEL_GATEWAY_PROVIDER_MODULES": "[]",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.stdout = StringIO()
        self.redirect = redirect_stdout(self.stdout)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.bindings = {}

    async def asyncTearDown(self):
        await close_models()

    async def start(self, *roles, concurrency=1):
        self.bindings = {
            role: ModelBinding(
                profile_id="local-" + role,
                execution="local",
                provider="local_process",
                model_id="fixture",
                model_revision="1",
                endpoint=None,
                credential_env=None,
                max_concurrency=concurrency,
                embedding_dimensions=3 if role == "embedding" else None,
            )
            for role in roles
        }
        os.environ["MODEL_GATEWAY_LOCAL_LOADERS"] = json.dumps(
            {
                binding.profile_id: "tests.model_gateway_fixtures:create_model"
                for binding in self.bindings.values()
            }
        )
        await start_models(self.bindings)

    def records(self, role="embedding"):
        folder = hashlib.sha256(self.bindings[role].profile_id.encode()).hexdigest()
        file = Path(self.temp.name) / folder / "fixture-calls.jsonl"
        return (
            [json.loads(line) for line in file.read_text().splitlines()]
            if file.exists()
            else []
        )

    async def drained(self):
        async with asyncio.timeout(8):
            while _manager._get_control().manager.roles["embedding"].jobs:
                await asyncio.sleep(0.01)

    async def test_pool_loads_once_in_children_and_duplicate_calls_share_inference(
        self,
    ):
        await self.start("embedding", concurrency=2)
        before = self.records()
        self.assertEqual(sum(row["operation"] == "load" for row in before), 2)
        self.assertTrue(all(row["pid"] != os.getpid() for row in before))
        results = await asyncio.gather(
            *(embed_texts(["DELAY"], "query", timeout_s=2) for _ in range(10))
        )
        self.assertTrue(all(result.vectors == [[5.0] * 3] for result in results))
        self.assertEqual(sum(row.get("marker") == "DELAY" for row in self.records()), 1)
        self.assertEqual(sum(row["operation"] == "load" for row in self.records()), 2)

    async def test_sync_generation_stream_is_collected_in_child(self):
        await self.start("generation")
        result = await generate(
            [ModelMessage(role="user", content="STREAM")],
            GenerationOptions(max_output_tokens=8, temperature=None),
            timeout_s=2,
        )
        self.assertEqual((result.text, result.finish_reason), ("OK", "completed"))

    async def test_invalid_local_stream_is_closed_before_error_is_returned(self):
        await self.start("generation")
        with self.assertRaisesRegex(StepError, "model_invalid_response"):
            await generate(
                [ModelMessage(role="user", content="BAD_STREAM")],
                GenerationOptions(max_output_tokens=8, temperature=None),
                timeout_s=2,
            )
        self.assertEqual(
            sum(row["operation"] == "stream_closed" for row in self.records("generation")),
            1,
        )

    async def test_local_transport_handles_all_role_payloads_and_hides_backend_output(
        self,
    ):
        await self.start(
            "embedding", "generation", "reranker", "injection", "ocr", "vision"
        )
        self.assertEqual(
            (await rerank("query", ["a", "b"], timeout_s=2)).scores[1].candidate_index,
            1,
        )
        self.assertEqual(
            (await classify_injection("question", timeout_s=2)).label, "benign"
        )
        image = ImageContent(data=b"\x00\xffbinary fixture", mime_type="image/png")
        self.assertEqual((await recognize_text(image, timeout_s=2)).text, "")
        self.assertEqual(
            (await describe_image(image, timeout_s=2)).description, "Fixture image"
        )
        self.assertNotIn("backend startup output", self.stdout.getvalue())

    async def test_missing_loader_is_configuration_error_and_start_can_recover(self):
        binding = ModelBinding(
            profile_id="missing",
            execution="local",
            provider="local_process",
            model_id="fixture",
            model_revision="1",
            endpoint=None,
            credential_env=None,
            max_concurrency=1,
            embedding_dimensions=3,
        )
        os.environ["MODEL_GATEWAY_LOCAL_LOADERS"] = (
            '{"missing":"missing_module:create_model"}'
        )
        with self.assertRaisesRegex(StepError, "model_configuration_error"):
            await start_models({"embedding": binding})
        await self.start("embedding")
        self.assertEqual(
            (await embed_texts(["ok"], "query", timeout_s=2)).vectors, [[2.0] * 3]
        )

    @unittest.skipUnless(
        sys.platform == "linux",
        "Process descendants are supervised in Linux containers",
    )
    async def test_timeout_stops_tree_and_retry_does_not_repeat_unknown_inference(self):
        await self.start("embedding")
        with self.assertRaisesRegex(StepError, "model_timeout"):
            await embed_texts(["SPAWN_HANG"], "query", timeout_s=0.3)
        await self.drained()
        records = self.records()
        old = next(row["pid"] for row in records if row.get("marker") == "SPAWN_HANG")
        child = next(row["child"] for row in records if row["operation"] == "child")
        self.assertTrue(_info(old) is None or _info(old)[1] == "Z")
        self.assertTrue(_info(child) is None or _info(child)[1] == "Z")
        with self.assertRaisesRegex(StepError, "model_unavailable"):
            await embed_texts(["SPAWN_HANG"], "query", timeout_s=1)
        self.assertEqual(
            sum(row.get("marker") == "SPAWN_HANG" for row in self.records()), 1
        )
        self.assertEqual(
            (await embed_texts(["healthy"], "query", timeout_s=2)).vectors, [[7.0] * 3]
        )

    async def test_close_stops_active_inference(self):
        await self.start("embedding")
        task = asyncio.create_task(embed_texts(["HANG"], "query", timeout_s=20))
        async with asyncio.timeout(3):
            while not any(row.get("marker") == "HANG" for row in self.records()):
                await asyncio.sleep(0.01)
        await close_models()
        with self.assertRaises(asyncio.CancelledError):
            await task
        for row in self.records():
            if row.get("marker") == "HANG" and sys.platform == "linux":
                self.assertTrue(
                    _info(row["pid"]) is None or _info(row["pid"])[1] == "Z"
                )
