"""Download only missing pinned Ollama weights, then probe through Model Gateway."""

import asyncio
import json
import time

import httpx

from deploy.config import active_bindings
from rag._configuration import environment_values
from rag.model_gateway.entrypoints import start_models, close_models
from rag.model_gateway.ollama import _model_name
from rag.runtime._config import RuntimeSettings


async def prepare_models():
    settings = RuntimeSettings()
    bindings = active_bindings(settings)
    values = environment_values()
    timeout = float(values.get("MODEL_DOWNLOAD_TIMEOUT_S", "3600"))
    models = {}
    for binding in bindings.values():
        models[_model_name(binding.model_id)] = binding.model_revision.removeprefix("sha256:")
    async with httpx.AsyncClient(base_url="http://ollama:11434", trust_env=False, timeout=30) as client:
        deadline = time.monotonic() + settings.startup_timeout_s
        while True:
            try:
                response = await client.get("/api/tags")
                response.raise_for_status()
                installed = {row["name"]: row["digest"].removeprefix("sha256:")
                             for row in response.json()["models"]}
                break
            except (httpx.HTTPError, ValueError, KeyError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Ollama did not become ready") from None
                await asyncio.sleep(2)
        # Validate all existing weights before changing the server's model set.
        for name, expected in models.items():
            if name in installed and installed[name] != expected:
                raise RuntimeError("Installed model revision differs from the pinned profile: " + name)
        for name, expected in sorted(models.items()):
            if name not in installed:
                print("Downloading model: " + name, flush=True)
                async with asyncio.timeout(timeout):
                    async with client.stream("POST", "/api/pull", json={"model": name, "stream": True},
                                             timeout=httpx.Timeout(120, connect=10)) as response:
                        response.raise_for_status()
                        completed = False
                        async for line in response.aiter_lines():
                            if not line:
                                continue
                            event = json.loads(line)
                            if "error" in event:
                                raise RuntimeError("Ollama model download failed: " + name)
                            completed = event.get("status") == "success"
                        if not completed:
                            raise RuntimeError("Incomplete Ollama model download: " + name)
                response = await client.get("/api/tags")
                response.raise_for_status()
                actual = {row["name"]: row["digest"].removeprefix("sha256:")
                          for row in response.json()["models"]}.get(name)
                if actual != expected:
                    raise RuntimeError("Downloaded model revision differs from the pinned profile: " + name)
    # start_models includes real role-specific inference probes and DTO validation.
    # No business data is used and inference always goes through Model Gateway.
    try:
        await start_models(bindings)
        print("Model Gateway probes passed for: " + ", ".join(sorted(bindings)), flush=True)
    finally:
        await close_models()
