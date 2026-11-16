"""Answer-only routing, cache isolation and OAuth/HTTP failure boundaries."""

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
import os
import ssl
import time
import unittest
from unittest.mock import patch

import httpx
from pydantic import SecretStr

from rag.contracts import StepError
from rag.model_gateway._gigachat_config import _Options
from rag.model_gateway.contracts import GenerationOptions, ModelMessage
from rag.model_gateway.entrypoints import start_models, close_models, generate
from rag.model_gateway.providers import unregister_provider
from rag.runtime._config import RuntimeSettings
from rag.runtime.runner import _bindings


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            "RAG_ENV_FILE": "nonexistent-gigachat-test.env", "GIGACHAT_MODEL": "test-model",
        }, clear=True))

    def settings(self, enabled=None):
        mapping = {role: dict(profile_id=role + "-local", provider="ollama", execution="remote",
                              model_id="local:tiny", model_revision="sha256:" + "a" * 64,
                              endpoint="http://ollama:11434", credential_env=None,
                              max_concurrency=1, embedding_dimensions=3 if role == "embedding" else None)
                   for role in ("embedding", "generation", "injection")}
        switch = {} if enabled is None else {"use_gigachat": enabled}
        return RuntimeSettings(_env_file=None, text_only=True, **switch,
                               embedding_profile="embedding-local", embedding_dimensions=3,
                               model_bindings=SecretStr(json.dumps(mapping)))

    def test_disabled_requires_no_api_configuration(self):
        os.environ.update(GIGACHAT_ENDPOINT="invalid", GIGACHAT_MODEL="")
        settings = self.settings()
        self.assertEqual(_bindings("questions", settings)["generation"].provider, "ollama")
        self.assertEqual(settings.cache_prefix, "rag:exact:v2:")

    def test_enabled_changes_only_answers_and_isolates_cache(self):
        settings = self.settings(True)
        question, worker = _bindings("questions", settings), _bindings("ingestion", settings)
        self.assertEqual(question["generation"].provider, "gigachat")
        self.assertEqual(worker["generation"].profile_id, "generation-local")
        self.assertEqual(question["embedding"], worker["embedding"])
        self.assertEqual(json.loads(settings.model_bindings.get_secret_value())["generation"]["provider"], "ollama")
        self.assertIn(question["generation"].profile_id, settings.cache_prefix)
        os.environ["GIGACHAT_MODEL"] = "another-model"
        self.assertNotEqual(settings.cache_prefix, self.settings(True).cache_prefix)
        self.assertEqual(self.settings().cache_prefix, "rag:exact:v2:")

    def test_enabled_requires_model_and_https(self):
        os.environ["GIGACHAT_MODEL"] = ""
        with self.assertRaises(StepError):
            self.settings(True)
        os.environ.update(GIGACHAT_MODEL="test-model", GIGACHAT_ENDPOINT="http://insecure")
        with self.assertRaises(ValueError):
            self.settings(True)

    def test_flag_from_environment_routes_answers_and_false_rolls_back(self):
        os.environ["RAG_USE_GIGACHAT"] = "true"
        self.assertEqual(_bindings("questions", self.settings())["generation"].provider, "gigachat")
        os.environ["RAG_USE_GIGACHAT"] = "false"
        self.assertEqual(_bindings("questions", self.settings())["generation"].provider, "ollama")


class GigaChatTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.dict(os.environ, {
            "RAG_ENV_FILE": "nonexistent-gigachat-test.env", "GIGACHAT_MODEL": "test-model",
            "GIGACHAT_CREDENTIALS": "test-credentials", "MODEL_GATEWAY_PROVIDER_MODULES": "[]",
            "MODEL_GATEWAY_STARTUP_TIMEOUT_S": "5",
        }, clear=True))
        self.output = StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.calls, self.override, self.auth_count = [], None, 0
        self.client_options = []
        original = httpx.AsyncClient
        def client(**kwargs):
            self.client_options.append(kwargs)
            return original(transport=httpx.MockTransport(self.handler), **kwargs)
        self.enterContext(patch("rag.model_gateway.gigachat.httpx.AsyncClient",
                                side_effect=client))

    async def asyncTearDown(self):
        await close_models()
        unregister_provider("gigachat")

    def response(self, reason="stop"):
        return dict(model="test-model", choices=[dict(
            message=dict(role="assistant", content='{"kind":"insufficient_evidence"}'),
            finish_reason=reason)], usage=dict(prompt_tokens=12, completion_tokens=8))

    async def handler(self, request):
        self.calls.append(request)
        if self.override:
            result = self.override(request)
            if hasattr(result, "__await__"):
                result = await result
            if result is not None:
                return result
        if request.url.path == "/api/v2/oauth":
            self.auth_count += 1
            return httpx.Response(200, json={"access_token": "token-" + str(self.auth_count),
                                            "expires_at": int((time.time() + 1800) * 1000)})
        return httpx.Response(200, json=self.response())

    async def start(self):
        await start_models({"generation": _Options()._binding()})

    def adapter(self):
        from rag.model_gateway._manager import _get_control
        return _get_control().manager.roles["generation"].adapter

    async def call(self, text="Question", timeout=3):
        return await generate([ModelMessage(role="user", content=text)],
                              GenerationOptions(max_output_tokens=128, temperature=0),
                              timeout_s=timeout)

    async def test_gateway_authentication_token_reuse_usage_and_secret_free_logs(self):
        await self.start()
        result = await self.call()
        self.assertEqual(self.auth_count, 1)
        self.assertEqual((result.usage.input_tokens, result.usage.output_tokens), (12, 8))
        auth, chat = self.calls[0], self.calls[-1]
        self.assertEqual(auth.headers["Authorization"], "Basic test-credentials")
        self.assertEqual(auth.content, b"scope=GIGACHAT_API_PERS")
        self.assertIn("rquid", auth.headers)
        self.assertEqual(chat.headers["Authorization"], "Bearer token-1")
        self.assertEqual(json.loads(chat.content)["max_tokens"], 128)
        self.assertFalse(json.loads(chat.content)["stream"])
        self.assertEqual(json.loads(chat.content)["response_format"],
                         {"type": "json_schema", "schema": {"type": "object"}})
        self.assertNotIn("test-credentials", self.output.getvalue())
        self.assertNotIn("Bearer token-1", self.output.getvalue())

    async def test_concurrent_calls_refresh_expired_token_once(self):
        os.environ["GIGACHAT_MAX_CONCURRENCY"] = "2"
        await self.start()
        self.adapter().expires_at = 0
        await asyncio.gather(self.call("one"), self.call("two"))
        self.assertEqual(self.auth_count, 2)

    async def test_401_refreshes_once_then_succeeds(self):
        await self.start()
        self.override = lambda request: (httpx.Response(401)
            if request.url.path.endswith("completions")
            and request.headers["Authorization"] == "Bearer token-1" else None)
        await self.call()
        self.assertEqual(self.auth_count, 2)

    async def test_second_401_is_permanent(self):
        await self.start()
        before = len(self.calls)
        self.override = lambda request: (httpx.Response(401)
                                        if request.url.path.endswith("completions") else None)
        with self.assertRaises(StepError) as caught:
            await self.call()
        self.assertEqual((caught.exception.code, caught.exception.retryable),
                         ("model_configuration_error", False))
        self.assertEqual(len(self.calls) - before, 3)

    async def test_transport_timeout_never_repeats_generation(self):
        await self.start()
        before = len(self.calls)
        def fail(request):
            raise httpx.ReadTimeout("untrusted response", request=request)
        self.override = fail
        with self.assertRaises(StepError) as caught:
            await self.call()
        self.assertEqual(caught.exception.code, "model_timeout")
        self.assertEqual(len(self.calls) - before, 1)

    async def test_oauth_and_chat_share_one_timeout(self):
        await self.start()
        self.adapter().expires_at = 0
        async def slow(request):
            await asyncio.sleep(0.07)
        self.override = slow
        with self.assertRaises(StepError) as caught:
            await self.call(timeout=0.1)
        self.assertEqual(caught.exception.code, "model_timeout")

    async def test_finish_reasons_and_statuses(self):
        await self.start()
        for reason, expected in (("length", "length"), ("blacklist", "filtered")):
            self.override = lambda request, reason=reason: httpx.Response(200, json=self.response(reason))
            self.assertEqual((await self.call(reason)).finish_reason, expected)
        for status, code, retryable in ((429, "model_overloaded", True),
                                        (503, "model_unavailable", True),
                                        (422, "model_invalid_input", False),
                                        (402, "model_configuration_error", False)):
            self.override = lambda request, status=status: httpx.Response(status)
            with self.assertRaises(StepError) as caught:
                await self.call(str(status))
            self.assertEqual((caught.exception.code, caught.exception.retryable), (code, retryable))

    async def test_invalid_and_oversized_responses(self):
        await self.start()
        for index, body in enumerate((b"not-json", b"[]", b'{"choices": []}')):
            self.override = lambda request, body=body: httpx.Response(200, content=body)
            with self.assertRaises(StepError) as caught:
                await self.call(str(index))
            self.assertEqual(caught.exception.code, "model_invalid_response")
        self.adapter().maximum = 10
        self.override = lambda request: httpx.Response(200, content=b"x" * 11)
        with self.assertRaises(StepError) as caught:
            await self.call("oversized")
        self.assertEqual(caught.exception.code, "model_invalid_response")

    async def test_missing_credentials_fail_without_network(self):
        os.environ.pop("GIGACHAT_CREDENTIALS")
        with self.assertRaises(StepError):
            await self.start()
        self.assertEqual(self.calls, [])

    async def test_missing_ca_is_configuration_error_without_network(self):
        os.environ["GIGACHAT_CA_BUNDLE"] = "nonexistent-ca.pem"
        with self.assertRaises(StepError) as caught:
            await self.start()
        self.assertEqual(caught.exception.code, "model_configuration_error")
        self.assertEqual(self.calls, [])

    async def test_explicit_ssl_opt_out_applies_to_oauth_and_chat_and_ignores_ca(self):
        os.environ.update(GIGACHAT_VERIFY_SSL_CERTS="false", GIGACHAT_CA_BUNDLE="absent.pem")
        await self.start()
        await self.call()
        self.assertIs(self.client_options[0]["verify"], False)
        self.assertEqual(self.calls[0].url.path, "/api/v2/oauth")
        self.assertEqual(self.calls[-1].url.path, "/v1/chat/completions")

    async def test_ssl_verification_is_enabled_by_default(self):
        await self.start()
        self.assertIsInstance(self.client_options[0]["verify"], ssl.SSLContext)
        self.assertEqual(self.client_options[0]["verify"].verify_mode, ssl.CERT_REQUIRED)
