"""Contracts: docs/documents/model-gateway/native-cpu.md.

Private-network transport for the pinned CPU OCR, reranker and injection models.
"""

import base64
import json
import os

import httpx

from rag.contracts import StepError
from rag.model_gateway.providers import ModelAdapter, register_provider


class _NativeAdapter(ModelAdapter):
    execution = "remote"

    def __init__(self, binding, role):
        self.binding, self.role = binding, role
        self.client = None
        self.ready = False
        if role not in {"ocr", "reranker", "injection"} or binding.execution != "remote":
            raise StepError("model_configuration_error", False)

    async def _request(self, method, path, payload=None, *, timeout_s):
        try:
            async with self.client.stream(method, path, json=payload, timeout=timeout_s) as response:
                if response.status_code != 200:
                    code = ("model_timeout" if response.status_code == 408
                            else "model_overloaded" if response.status_code == 429
                            else "model_configuration_error" if response.status_code in (401, 403, 404, 409)
                            else "model_invalid_input" if response.status_code in (400, 413, 422)
                            else "model_unavailable")
                    raise StepError(code, code in {"model_timeout", "model_overloaded", "model_unavailable"})
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > self.maximum:
                        raise StepError("model_invalid_response", False)
                result = json.loads(body)
                if not isinstance(result, dict):
                    raise StepError("model_invalid_response", False)
                return result
        except httpx.TimeoutException:
            raise StepError("model_timeout", True) from None
        except httpx.HTTPError:
            raise StepError("model_unavailable", True) from None
        except (ValueError, TypeError):
            raise StepError("model_invalid_response", False) from None

    async def start(self):
        from rag.model_gateway._config import _Settings
        if not self.binding.credential_env or not os.environ.get(self.binding.credential_env):
            raise StepError("model_configuration_error", False)
        settings = _Settings()
        self.maximum = settings.max_message_bytes
        self.client = httpx.AsyncClient(base_url=self.binding.endpoint.rstrip("/"), trust_env=False,
                                       follow_redirects=False,
                                       limits=httpx.Limits(max_connections=self.binding.max_concurrency,
                                                           max_keepalive_connections=self.binding.max_concurrency),
                                       headers={"Authorization": "Bearer " + os.environ[self.binding.credential_env]})
        metadata = await self._request("GET", "/v1/models/" + self.role, timeout_s=settings.startup_timeout_s)
        if any(metadata.get(key) != getattr(self.binding, key) for key in ("model_id", "model_revision", "profile_id")):
            raise StepError("model_profile_mismatch", False)
        self.ready = True

    async def invoke(self, operation, payload, *, timeout_s):
        if not self.ready:
            raise StepError("model_not_ready", False)
        encoded = dict(payload)
        if self.role == "ocr":
            encoded["image"] = {**payload["image"], "data": base64.b64encode(payload["image"]["data"]).decode("ascii")}
        return await self._request("POST", "/v1/invoke/" + self.role,
                                   {"operation": operation, "profile_id": self.binding.profile_id, "payload": encoded},
                                   timeout_s=timeout_s)

    async def close(self):
        self.ready = False
        if self.client is not None:
            await self.client.aclose()


def register_providers() -> None:
    """Register transport only; downloading/loading happens in the explicit CPU service."""
    register_provider("native_cpu", _NativeAdapter)
