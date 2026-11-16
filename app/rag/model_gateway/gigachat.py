"""Contracts: docs/documents/model-gateway/gigachat.md.

Answer-only GigaChat HTTP adapter. Imports never authenticate or call a model.
"""

import asyncio
import json
import math
import os
import ssl
import time
from uuid import uuid4

import httpx

from rag.contracts import StepError
from rag.model_gateway._config import _Settings
from rag.model_gateway._gigachat_config import _Options
from rag.model_gateway.contracts import TokenUsage
from rag.model_gateway.providers import ModelAdapter, register_provider


def _invalid():
    raise StepError("model_invalid_response", False)


class _GigaChatAdapter(ModelAdapter):
    execution = "remote"

    def __init__(self, binding, role):
        if role != "generation" or binding.execution != "remote":
            raise StepError("model_configuration_error", False)
        self.binding = binding
        self.client = None
        self.ready = False
        self.token = None
        self.expires_at = 0
        self.token_lock = asyncio.Lock()

    async def start(self):
        self.options = _Options()
        if self.binding != self.options._binding():
            raise StepError("model_configuration_error", False)
        self.credentials = os.environ.get(self.binding.credential_env, "").strip()
        if not self.credentials:
            raise StepError("model_configuration_error", False)
        self.maximum = _Settings().max_message_bytes
        try:
            # An explicit opt-out is scoped to this provider's HTTP client.
            context = False
            if self.options.verify_ssl_certs:
                context = ssl.create_default_context()
                if self.options.ca_bundle is not None:
                    context.load_verify_locations(cafile=str(self.options.ca_bundle))
            self.client = httpx.AsyncClient(
                verify=context, trust_env=False, follow_redirects=False,
                limits=httpx.Limits(max_connections=self.binding.max_concurrency,
                                   max_keepalive_connections=self.binding.max_concurrency),
            )
        except (OSError, ValueError):
            raise StepError("model_configuration_error", False) from None
        self.ready = True

    async def _request(self, url, *, timeout_s, headers, payload=None, form=None,
                       accept_unauthorized=False):
        try:
            async with self.client.stream("POST", url, headers=headers, json=payload,
                                          data=form, timeout=timeout_s) as response:
                status = response.status_code
                if status == 401 and accept_unauthorized:
                    return None
                if status != 200:
                    code = ("model_timeout" if status == 408 else
                            "model_overloaded" if status == 429 else
                            "model_unavailable" if status >= 500 else
                            "model_configuration_error" if status in (401, 402, 403, 404) else
                            "model_invalid_input")
                    raise StepError(code, code in {
                        "model_timeout", "model_overloaded", "model_unavailable"})
                body = bytearray()
                async for part in response.aiter_bytes():
                    body.extend(part)
                    if len(body) > self.maximum:
                        _invalid()
            result = json.loads(body)
            if not isinstance(result, dict):
                _invalid()
            return result
        except httpx.TimeoutException:
            raise StepError("model_timeout", True) from None
        except httpx.TransportError:
            raise StepError("model_unavailable", True) from None
        except (ValueError, TypeError):
            raise StepError("model_invalid_response", False) from None

    async def _access_token(self, timeout_s, rejected=None):
        async with self.token_lock:
            if (self.token is not None and self.token != rejected
                    and self.expires_at > time.time() + 30):
                return self.token
            result = await self._request(
                self.options.auth_url, timeout_s=timeout_s,
                headers={"Authorization": "Basic " + self.credentials,
                         "RqUID": str(uuid4()), "Accept": "application/json"},
                form={"scope": self.options.scope},
            )
            token, expiry = result.get("access_token"), result.get("expires_at")
            if (not isinstance(token, str) or not token.strip()
                    or type(expiry) not in (int, float) or not math.isfinite(expiry)):
                _invalid()
            # OAuth installations/documentation use Unix milliseconds or seconds.
            expiry = expiry / 1000 if expiry > 100_000_000_000 else expiry
            if expiry <= time.time() + 30:
                _invalid()
            self.token, self.expires_at = token, expiry
            return self.token

    async def invoke(self, operation, payload, *, timeout_s):
        if not self.ready:
            raise StepError("model_not_ready", False)
        if operation != "generate":
            raise StepError("model_invalid_input", False)
        async with asyncio.timeout(timeout_s):
            options = payload["options"]
            request = dict(model=self.binding.model_id, messages=payload["messages"],
                           stream=False, max_tokens=options["max_output_tokens"],
                           response_format={"type": "json_schema", "schema": {"type": "object"}})
            if options["temperature"] is not None:
                request["temperature"] = options["temperature"]
            token = await self._access_token(timeout_s)
            result = await self._request(
                self.binding.endpoint + "/chat/completions", timeout_s=timeout_s,
                headers={"Authorization": "Bearer " + token}, payload=request,
                accept_unauthorized=True,
            )
            if result is None:
                # A definite 401 is safe to retry once after refreshing auth.
                # Timeouts/transport failures never repeat the generation POST.
                token = await self._access_token(timeout_s, rejected=token)
                result = await self._request(
                    self.binding.endpoint + "/chat/completions", timeout_s=timeout_s,
                    headers={"Authorization": "Bearer " + token}, payload=request,
                )
            choices = result.get("choices")
            if not isinstance(choices, list) or len(choices) != 1:
                _invalid()
            choice = choices[0]
            if not isinstance(choice, dict):
                _invalid()
            message = choice.get("message")
            if (not isinstance(message, dict) or message.get("role") != "assistant"
                    or not isinstance(message.get("content"), str)
                    or message.get("function_call") or message.get("tool_calls")):
                _invalid()
            reason = {"stop": "completed", "length": "length",
                      "blacklist": "filtered", "content_filter": "filtered"}.get(
                          choice.get("finish_reason"))
            if reason is None:
                _invalid()
            usage = result.get("usage")
            if usage is not None:
                if not isinstance(usage, dict):
                    _invalid()
                try:
                    usage = TokenUsage(input_tokens=usage.get("prompt_tokens"),
                                       output_tokens=usage.get("completion_tokens")).model_dump()
                except ValueError:
                    _invalid()
            return dict(profile_id=self.binding.profile_id, text=message["content"],
                        finish_reason=reason, usage=usage)

    async def probe(self, operation, payload, *, timeout_s):
        # The generic Gateway probe asks for plain OK with only eight tokens.
        # This answer-only backend uses JSON mode for every generation.
        request = dict(messages=[dict(role="user", content=
                       'Return exactly this JSON object: {"kind":"insufficient_evidence"}')],
                       options=dict(max_output_tokens=64, temperature=0))
        return [await self.invoke(operation, request, timeout_s=timeout_s)]

    async def close(self):
        self.ready = False
        self.token = None
        self.credentials = ""
        if self.client is not None:
            await self.client.aclose()


def register_providers() -> None:
    """Register the explicitly selected answer provider without network access."""
    register_provider("gigachat", _GigaChatAdapter)
