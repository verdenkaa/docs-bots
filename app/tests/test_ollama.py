"""Ollama HTTP contract, immutable profiles and real Gateway admission behavior."""

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import unittest
from unittest.mock import patch

import httpx

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
from rag.model_gateway.providers import unregister_provider
from rag.model_gateway.ollama_setup import _environment


class OllamaTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "MODEL_GATEWAY_PROVIDER_MODULES": '["rag.model_gateway.ollama"]',
                "MODEL_GATEWAY_STARTUP_TIMEOUT_S": "5",
                "OLLAMA_GATEWAY_KEEP_ALIVE": "0",
                "OLLAMA_GATEWAY_TEXT_CONTEXT_LENGTH": "2048",
                "OLLAMA_GATEWAY_VISION_CONTEXT_LENGTH": "2048",
                "OLLAMA_GATEWAY_GENERATION_JSON": "false",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        self.stdout = StringIO()
        self.redirect = redirect_stdout(self.stdout)
        self.redirect.__enter__()
        self.addCleanup(self.redirect.__exit__, None, None, None)
        self.models = {
            "embeddinggemma:300m": ["embedding"],
            "qwen3:0.6b": ["completion", "thinking"],
            "vision:tiny": ["completion", "vision"],
        }
        self.digest = "a" * 64
        self.requests = []
        self.override = None
        original = httpx.AsyncClient
        self.mock = patch(
            "rag.model_gateway.ollama.httpx.AsyncClient",
            side_effect=lambda **kwargs: original(
                transport=httpx.MockTransport(self.handler), **kwargs
            ),
        )
        self.mock.start()
        self.addCleanup(self.mock.stop)
        self.bindings = {}

    async def asyncTearDown(self):
        await close_models()
        unregister_provider("ollama")

    async def handler(self, request):
        body = json.loads(request.content) if request.content else {}
        self.requests.append((request.method, request.url.path, body))
        if self.override is not None:
            result = self.override(request, body)
            if hasattr(result, "__await__"):
                result = await result
            if result is not None:
                return result
        if request.url.path == "/api/tags":
            return httpx.Response(
                200,
                json={
                    "models": [
                        dict(name=name, digest=self.digest) for name in self.models
                    ]
                },
            )
        if request.url.path == "/api/show":
            return httpx.Response(
                200, json=dict(capabilities=self.models[body["model"]])
            )
        if request.url.path == "/api/embed":
            return httpx.Response(
                200,
                json=dict(
                    model=body["model"],
                    embeddings=[[1.0, 2.0, 3.0] for _ in body["input"]],
                ),
            )
        if request.url.path == "/api/chat":
            if body.get("format") == "json":
                content = '{"questions":[]}'
            elif "format" in body:
                properties = body["format"]["properties"]
                if "label" in properties:
                    value = {"label": "benign"}
                else:
                    count = len(
                        json.loads(body["messages"][-1]["content"])["candidates"]
                    )
                    value = {
                        "scores": [
                            dict(candidate_index=n, score=0.5)
                            for n in reversed(range(count))
                        ]
                    }
                content = json.dumps(value)
            else:
                content = "HELLO" if "images" in body["messages"][0] else "OK"
            return httpx.Response(
                200,
                json=dict(
                    model=body["model"],
                    done=True,
                    done_reason="stop",
                    message=dict(role="assistant", content=content),
                    prompt_eval_count=10,
                    eval_count=2,
                ),
            )
        raise AssertionError("Unexpected Ollama operation")

    async def start(self, *roles):
        self.bindings = {
            role: ModelBinding(
                profile_id="fixture-" + role,
                execution="remote",
                provider="ollama",
                model_id=(
                    "embeddinggemma:300m"
                    if role == "embedding"
                    else "vision:tiny" if role in ("vision", "ocr") else "qwen3:0.6b"
                ),
                model_revision="sha256:" + self.digest,
                endpoint="http://ollama.test:11434",
                credential_env=None,
                max_concurrency=1,
                embedding_dimensions=3 if role == "embedding" else None,
            )
            for role in roles
        }
        await start_models(self.bindings)
        self.requests.clear()

    async def test_all_six_roles_use_real_protocol_and_purpose_prefixes(self):
        await self.start(
            "embedding", "generation", "injection", "reranker", "ocr", "vision"
        )
        await embed_texts(["Question"], "query", timeout_s=1)
        await embed_texts(["Document"], "document", timeout_s=1)
        batches = [body for _, path, body in self.requests if path == "/api/embed"]
        self.assertEqual(batches[0]["input"], ["task: search result | query: Question"])
        self.assertEqual(batches[1]["input"], ["title: none | text: Document"])
        self.assertTrue(all(body["truncate"] is False for body in batches))
        result = await generate(
            [ModelMessage(role="user", content="Question")],
            GenerationOptions(max_output_tokens=10, temperature=0.2),
            timeout_s=1,
        )
        self.assertEqual((result.text, result.usage.output_tokens), ("OK", 2))
        self.assertEqual(
            (await classify_injection("Question", timeout_s=1)).label, "benign"
        )
        self.assertEqual(
            [
                score.candidate_index
                for score in (await rerank("Question", ["a", "b"], timeout_s=1)).scores
            ],
            [0, 1],
        )
        image = ImageContent(data=b"fixture", mime_type="image/png")
        self.assertEqual((await recognize_text(image, timeout_s=1)).text, "HELLO")
        self.assertEqual(
            (await describe_image(image, timeout_s=1)).description, "HELLO"
        )
        chats = [body for _, path, body in self.requests if path == "/api/chat"]
        self.assertTrue(
            all(body["stream"] is False and body["keep_alive"] == 0 for body in chats)
        )
        self.assertTrue(chats[0]["think"] is False)
        self.assertNotIn("think", chats[-1])

    async def test_numeric_keep_alive_is_sent_as_json_seconds_for_embed_and_chat(self):
        for configured, expected in (("-1", -1), ("0", 0), ("3600", 3600),
                                     ("0.5", 0.5), ("5m", "5m"), ("-1s", "-1s")):
            with self.subTest(configured=configured):
                with patch.dict(os.environ, {"OLLAMA_GATEWAY_KEEP_ALIVE": configured}):
                    await self.start("embedding", "generation")
                await embed_texts(["synthetic probe"], "query", timeout_s=1)
                await generate([ModelMessage(role="user", content="synthetic probe")],
                               GenerationOptions(max_output_tokens=8, temperature=0), timeout_s=1)
                calls = [body for _, path, body in self.requests if path in {"/api/embed", "/api/chat"}]
                self.assertEqual(len(calls), 2)
                self.assertTrue(all(body["keep_alive"] == expected for body in calls))
                if isinstance(expected, (int, float)):
                    self.assertTrue(all(type(body["keep_alive"]) is type(expected) for body in calls))
                await close_models()

    async def test_bge_m3_does_not_receive_embeddinggemma_instructions(self):
        self.models["bge-m3:567m"] = ["embedding"]
        await self.start("embedding")
        await close_models()
        self.bindings["embedding"] = self.bindings["embedding"].model_copy(update={
            "model_id": "bge-m3:567m", "profile_id": "bge-m3-fixture"})
        await start_models(self.bindings)
        self.requests.clear()
        await embed_texts(["Question"], "query", timeout_s=1)
        await embed_texts(["Document"], "document", timeout_s=1)
        batches = [body for _, path, body in self.requests if path == "/api/embed"]
        self.assertEqual([body["input"] for body in batches], [["Question"], ["Document"]])
        self.assertTrue(all(body["truncate"] is False for body in batches))

    async def test_vision_context_is_configured_independently_from_text(self):
        with patch.dict(os.environ, {"OLLAMA_GATEWAY_VISION_CONTEXT_LENGTH": "8192"}):
            await self.start("generation", "vision")
        self.assertEqual(_manager._get_control().manager.roles["vision"].adapter.context_length, 8192)
        self.assertEqual(_manager._get_control().manager.roles["generation"].adapter.context_length, 2048)

    async def test_changed_digest_rejects_inference_before_post(self):
        await self.start("embedding")
        self.digest = "b" * 64
        with self.assertRaisesRegex(StepError, "model_profile_mismatch"):
            await embed_texts(["Question"], "query", timeout_s=1)
        self.assertFalse(any(path == "/api/embed" for _, path, _ in self.requests))

    async def test_simultaneous_duplicates_and_retry_send_one_inference(self):
        await self.start("embedding")
        results = await asyncio.gather(
            *(embed_texts(["same"], "query", timeout_s=2) for _ in range(12))
        )
        self.assertTrue(all(result == results[0] for result in results))
        await embed_texts(["same"], "query", timeout_s=1)
        self.assertEqual(sum(path == "/api/embed" for _, path, _ in self.requests), 1)

    async def test_bad_terminal_wrong_model_and_invalid_json_are_not_successes(self):
        await self.start("generation")
        values = [
            dict(model="qwen3:0.6b", done=False),
            dict(model="wrong:tag", done=True),
            dict(model="qwen3:0.6b", done=True, done_reason="unknown"),
        ]
        for index, value in enumerate(values):
            self.override = lambda request, body, value=value: (
                httpx.Response(200, json=value)
                if request.url.path == "/api/chat"
                else None
            )
            with self.assertRaisesRegex(StepError, "model_invalid_response"):
                await generate(
                    [ModelMessage(role="user", content=str(index))],
                    GenerationOptions(max_output_tokens=8, temperature=0),
                    timeout_s=1,
                )
        self.override = lambda request, body: (
            httpx.Response(200, content=b"not json")
            if request.url.path == "/api/chat"
            else None
        )
        with self.assertRaisesRegex(StepError, "model_invalid_response"):
            await generate(
                [ModelMessage(role="user", content="bad json")],
                GenerationOptions(max_output_tokens=8, temperature=0),
                timeout_s=1,
            )

    async def test_transport_error_is_not_retried_and_does_not_leak_response(self):
        await self.start("embedding")
        self.override = lambda request, body: (
            httpx.Response(503, text="PRIVATE SERVER DETAIL")
            if request.url.path == "/api/embed"
            else None
        )
        with self.assertRaisesRegex(StepError, "model_unavailable"):
            await embed_texts(["same"], "query", timeout_s=1)
        async with asyncio.timeout(2):
            while _manager._get_control().manager.roles["embedding"].jobs:
                await asyncio.sleep(0)
        with self.assertRaisesRegex(StepError, "model_unavailable"):
            await embed_texts(["same"], "query", timeout_s=1)
        self.assertEqual(sum(path == "/api/embed" for _, path, _ in self.requests), 1)
        self.assertNotIn("PRIVATE SERVER DETAIL", self.stdout.getvalue())

    async def test_over_budget_chat_is_rejected_before_inference(self):
        await self.start("generation")
        with self.assertRaisesRegex(StepError, "model_invalid_input"):
            await generate(
                [ModelMessage(role="user", content="x" * 3000)],
                GenerationOptions(max_output_tokens=8, temperature=0),
                timeout_s=1,
            )
        self.assertFalse(any(path == "/api/chat" for _, path, _ in self.requests))

    async def test_empty_image_reply_is_valid_ocr_but_invalid_description(self):
        await self.start("ocr", "vision")
        self.override = lambda request, body: (
            httpx.Response(200, json=dict(model=body["model"], done=True, done_reason="stop",
                message=dict(role="assistant", content="")))
            if request.url.path == "/api/chat" else None
        )
        image = ImageContent(data=b"fixture", mime_type="image/png")
        self.assertEqual((await recognize_text(image, timeout_s=1)).text, "")
        with self.assertRaisesRegex(StepError, "model_invalid_response"):
            await describe_image(image, timeout_s=1)

    async def test_explicit_text_context_controls_admission_and_server_limit(self):
        with patch.dict(os.environ, {"OLLAMA_GATEWAY_TEXT_CONTEXT_LENGTH": "4096"}):
            await self.start("generation", "vision")
        await generate([ModelMessage(role="user", content="x" * 3000)],
                       GenerationOptions(max_output_tokens=8, temperature=0), timeout_s=1)
        call = next(body for _, path, body in self.requests if path == "/api/chat")
        self.assertEqual(call["options"]["num_ctx"], 4096)
        self.assertEqual(_manager._get_control().manager.roles["vision"].adapter.context_length, 2048)
        before = sum(path == "/api/chat" for _, path, _ in self.requests)
        with self.assertRaisesRegex(StepError, "model_invalid_input"):
            await generate([ModelMessage(role="user", content="x" * 4200)],
                           GenerationOptions(max_output_tokens=8, temperature=0), timeout_s=1)
        self.assertEqual(sum(path == "/api/chat" for _, path, _ in self.requests), before)

    async def test_json_generation_profile_requests_json_without_changing_other_roles(self):
        with patch.dict(os.environ, {"OLLAMA_GATEWAY_GENERATION_JSON": "true"}):
            await self.start("generation", "injection")
        await generate([ModelMessage(role="user", content='Return {"questions":[]}')],
                       GenerationOptions(max_output_tokens=32, temperature=None), timeout_s=1)
        call = next(body for _, path, body in self.requests if path == "/api/chat")
        self.assertEqual(call["format"], "json")
        self.assertEqual(call["options"]["temperature"], 0)
        self.assertFalse(_manager._get_control().manager.roles["injection"].adapter.generation_json)

    async def test_profile_file_values_round_trip_through_dotenv(self):
        import tempfile
        from pathlib import Path
        from dotenv import dotenv_values

        await self.start("embedding")
        values = _environment(self.bindings)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / ".env.ollama.local"
            path.write_text(
                "\n".join(
                    key + "=" + json.dumps(value) for key, value in values.items()
                ),
                encoding="utf-8",
            )
            self.assertEqual(dotenv_values(path), values)
