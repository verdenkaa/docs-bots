"""Native CPU transport admission, identity checks and sensitive payload handling."""

import base64
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch

import httpx

from rag.contracts import ImageContent, StepError
from rag.model_gateway.contracts import ModelBinding
from rag.model_gateway.entrypoints import (
    start_models, close_models, recognize_text, rerank, classify_injection,
)
from rag.model_gateway.providers import unregister_provider


class NativeCpuTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(os.environ, {
            "MODEL_GATEWAY_PROVIDER_MODULES": '["rag.model_gateway.native_cpu"]',
            "NATIVE_MODEL_TOKEN": "synthetic-private-token",
            "MODEL_GATEWAY_STARTUP_TIMEOUT_S": "5",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.output = StringIO()
        self.logs = redirect_stdout(self.output)
        self.logs.__enter__()
        self.addCleanup(self.logs.__exit__, None, None, None)
        self.requests, self.override = [], None
        self.bindings = {role: ModelBinding(
            profile_id=role + "-v1", execution="remote", provider="native_cpu",
            model_id="fixture/" + role, model_revision="a" * 40,
            endpoint="http://native.test:8090", credential_env="NATIVE_MODEL_TOKEN",
            max_concurrency=1, embedding_dimensions=None,
        ) for role in ("ocr", "reranker", "injection")}
        original = httpx.AsyncClient
        mock = patch("rag.model_gateway.native_cpu.httpx.AsyncClient", side_effect=lambda **kw:
                     original(transport=httpx.MockTransport(self.respond), **kw))
        mock.start()
        self.addCleanup(mock.stop)

    async def asyncTearDown(self):
        await close_models()
        unregister_provider("native_cpu")

    def respond(self, request):
        self.assertEqual(request.headers["Authorization"], "Bearer synthetic-private-token")
        self.requests.append(request)
        if self.override:
            response = self.override(request)
            if response is not None:
                return response
        role = request.url.path.rsplit("/", 1)[-1]
        binding = self.bindings[role]
        if request.method == "GET":
            return httpx.Response(200, json={key: getattr(binding, key) for key in
                                            ("model_id", "model_revision", "profile_id")})
        payload = json.loads(request.content)["payload"]
        if role == "ocr":
            self.assertIsInstance(base64.b64decode(payload["image"]["data"], validate=True), bytes)
            result = {"text": "Текст инструкции"}
        elif role == "reranker":
            result = {"scores": [{"candidate_index": n, "score": (n + 1) / 10}
                                 for n in range(len(payload["candidates"]))]}
        else:
            result = {"score": 0.95, "label": None}
        return httpx.Response(200, json={"profile_id": binding.profile_id, **result})

    async def test_all_native_roles_probe_and_return_existing_dtos(self):
        await start_models(self.bindings)
        self.assertEqual(len([r for r in self.requests if r.method == "POST"]), 3)
        image = ImageContent(data=b"synthetic-image", mime_type="image/png")
        self.assertEqual((await recognize_text(image, timeout_s=1)).text, "Текст инструкции")
        scores = await rerank("question", ["first", "second"], timeout_s=1)
        self.assertEqual([s.candidate_index for s in scores.scores], [0, 1])
        decision = await classify_injection("private-question", timeout_s=1)
        self.assertIsNone(decision.label)
        self.assertEqual(decision.score, 0.95)
        self.assertNotIn("private-question", self.output.getvalue())
        self.assertNotIn("synthetic-private-token", self.output.getvalue())

    async def test_wrong_model_revision_blocks_startup(self):
        self.override = lambda request: httpx.Response(200, json={"model_revision": "wrong"})
        with self.assertRaises(StepError) as error:
            await start_models({"injection": self.bindings["injection"]})
        self.assertEqual(error.exception.code, "model_profile_mismatch")

    async def test_sensitive_error_body_is_not_propagated(self):
        await start_models({"injection": self.bindings["injection"]})
        self.override = lambda request: httpx.Response(503, text="private-question synthetic-private-token")
        with self.assertRaises(StepError) as error:
            await classify_injection("private-question", timeout_s=1)
        self.assertEqual(error.exception.code, "model_unavailable")
        self.assertTrue(error.exception.retryable)
        self.assertNotIn("private-question", str(error.exception) + self.output.getvalue())

    async def test_response_under_another_profile_is_rejected(self):
        await start_models({"injection": self.bindings["injection"]})
        self.override = lambda request: httpx.Response(200, json={"profile_id": "wrong", "score": .5, "label": None})
        with self.assertRaises(StepError) as error:
            await classify_injection("question", timeout_s=1)
        self.assertEqual(error.exception.code, "model_profile_mismatch")
