"""Contracts: docs/documents/model-gateway/injection-detector.md.

Explicit HTTP provider for the injection detector service (app/injection_detector).
Imports never open connections or load models.
"""

import json
import math

import httpx

from rag.contracts import StepError
from rag.model_gateway._config import _Settings
from rag.model_gateway.providers import ModelAdapter, register_provider

_TEMPORARY = {"model_timeout", "model_overloaded", "model_unavailable"}


def _invalid():
    raise StepError("model_invalid_response", False)


class _DetectorAdapter(ModelAdapter):
    execution = "remote"

    def __init__(self, binding, role):
        self.binding = binding
        self.client = None
        # The service has no authentication: a configured secret would give a false sense of protection.
        if (
            role != "injection"
            or binding.execution != "remote"
            or not binding.endpoint
            or binding.credential_env
        ):
            raise StepError("model_configuration_error", False)

    async def _request(self, method, path, payload=None, *, timeout_s):
        try:
            async with self.client.stream(
                method,
                self.binding.endpoint.rstrip("/") + path,
                json=payload,
                timeout=timeout_s,
            ) as response:
                status = response.status_code
                if status != 200:
                    code = (
                        "model_timeout" if status == 408
                        else "model_overloaded" if status == 429
                        else "model_unavailable" if status >= 500
                        else "model_configuration_error" if status in (401, 403, 404)
                        else "model_invalid_input"
                    )
                    raise StepError(code, code in _TEMPORARY)
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.maximum:
                        _invalid()
            result = json.loads(body)
            if not isinstance(result, dict):
                _invalid()
        except httpx.TimeoutException:
            raise StepError("model_timeout", True) from None
        except httpx.TransportError:
            raise StepError("model_unavailable", True) from None
        except (ValueError, TypeError):
            raise StepError("model_invalid_response", False) from None
        # Thresholds in Guardrails are tied to the profile: another model or revision behind the
        # same endpoint must not answer under it.
        if (
            result.get("model_id") != self.binding.model_id
            or result.get("model_revision") != self.binding.model_revision
        ):
            raise StepError("model_profile_mismatch", False)
        return result

    async def start(self):
        settings = _Settings()
        self.maximum = settings.max_message_bytes
        self.client = httpx.AsyncClient(
            trust_env=False,
            follow_redirects=False,
            limits=httpx.Limits(
                max_connections=self.binding.max_concurrency,
                max_keepalive_connections=self.binding.max_concurrency,
            ),
        )
        await self._request("GET", "/health/ready", timeout_s=settings.startup_timeout_s)

    async def invoke(self, operation, payload, *, timeout_s):
        if operation != "classify_injection":
            raise StepError("model_invalid_input", False)
        result = await self._request(
            "POST", "/classify", dict(text=payload["text"]), timeout_s=timeout_s
        )
        score = result.get("score")
        if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(score):
            _invalid()
        # Score only: with a label Guardrails would ignore its thresholds.
        return dict(profile_id=self.binding.profile_id, label=None, score=float(score))

    async def close(self):
        if self.client is not None:
            await self.client.aclose()


def register_providers() -> None:
    """Explicit startup hook for MODEL_GATEWAY_PROVIDER_MODULES."""
    register_provider("injection_detector", _DetectorAdapter)
