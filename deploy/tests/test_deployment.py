"""Deployment failure boundaries; no real credentials, Telegram calls or model downloads."""

import asyncio
import json
import os
from pathlib import Path
import runpy
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from pydantic import SecretStr

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "app"))

from deploy import config, models, server
from deploy.proxy import main as proxy
from rag.runtime._config import RuntimeSettings
from rag.runtime import _process_status


def binding(role, model="embeddinggemma:300m", revision="a" * 64):
    return dict(profile_id=role + "-profile", execution="remote", provider="ollama",
                model_id=model, model_revision="sha256:" + revision,
                endpoint="http://ollama:11434", credential_env=None, max_concurrency=1,
                embedding_dimensions=768 if role == "embedding" else None)


def settings():
    mapping = {"embedding": binding("embedding"),
               "generation": binding("generation", "qwen3:0.6b"),
               "injection": binding("injection", "qwen3:0.6b")}
    return RuntimeSettings(_env_file=None, text_only=True, use_reranker=False,
                           embedding_profile="embedding-profile", embedding_dimensions=768,
                           model_bindings=SecretStr(json.dumps(mapping)))


class ConfigurationTests(unittest.TestCase):
    def test_gigachat_keeps_local_ingestion_generation_in_preparation(self):
        with patch.dict(os.environ, {"GIGACHAT_MODEL": "test-model"}):
            item = settings()
            item.use_gigachat = True
            from rag.runtime.runner import _bindings

            self.assertEqual(_bindings("questions", item)["generation"].provider, "gigachat")
            self.assertEqual(config.active_bindings(item)["generation"].provider, "ollama")

    def test_proxy_check_does_not_start_services_when_imported(self):
        with patch.object(subprocess, "run", side_effect=AssertionError("Unexpected process on import")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("Unexpected process on import")):
            namespace = runpy.run_path(str(ROOT / "deploy/tests/check_proxy_image.py"))
        self.assertTrue(callable(namespace["main"]))

    def test_public_host_rejects_private_addresses_and_config_injection(self):
        for host in ("", "127.0.0.1", "10.0.0.1", "::1", "example.test", "example.org;return 200", "https://example.org"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                config.public_host(host)
        self.assertEqual(config.public_host("176.53.246.245"), "176.53.246.245")

    def test_disabled_reranker_is_not_downloaded_or_probed(self):
        item = settings()
        value = json.loads(item.model_bindings.get_secret_value())
        value["reranker"] = binding("reranker", "unused:latest")
        item.model_bindings = SecretStr(json.dumps(value))
        self.assertEqual(set(config.active_bindings(item)), {"embedding", "generation", "injection"})
        item.use_reranker = True
        self.assertIn("reranker", config.active_bindings(item))

    def test_ip_certificate_requests_shortlived_profile_without_staging(self):
        command = proxy.issue_command({"PUBLIC_HOST": "176.53.246.245"})
        self.assertEqual(command[command.index("--ip-address") + 1], "176.53.246.245")
        self.assertEqual(command[command.index("--required-profile") + 1], "shortlived")
        self.assertNotIn("--staging", command)
        self.assertIn("--register-unsafely-without-email", command)

    def test_nginx_routes_upload_separately_without_body_spooling_or_post_retries(self):
        template = (ROOT / "deploy/proxy/https.conf").read_text()
        rendered = proxy.render(template, {"PUBLIC_HOST": "176.53.246.245", "RAG_INGESTION_UPLOAD_TIMEOUT_S": "3600", "INGESTION_PROXY_TIMEOUT_S": "3660"})
        self.assertNotIn("@PUBLIC_HOST@", rendered)
        self.assertIn("proxy_read_timeout 3660s;", rendered)
        self.assertIn("client_max_body_size 20971520;", rendered)
        self.assertIn("proxy_request_buffering off;", rendered)
        self.assertIn("proxy_next_upstream off;", rendered)
        self.assertIn("location = /admin/ingestion/upload", rendered)
        self.assertIn("proxy_pass $worker;", rendered)
        self.assertIn("proxy_pass $core;", rendered)
        self.assertIn("$request_uri", rendered)

    def test_http_workers_do_not_launch_background_roles(self):
        commands = server.commands("core", {"API_WORKERS": "3"})
        self.assertEqual(set(commands), {"http", "relay", "recovery", "questions", "results", "maintenance"})
        self.assertEqual(commands["http"][commands["http"].index("--workers") + 1], "3")
        worker = server.commands("worker", {})
        self.assertEqual(worker["ingestion"][-3:], ["-m", "rag.runtime", "worker"])

    def test_mini_app_proxy_preserves_core_and_ingestion_routes(self):
        template = (ROOT / "deploy/proxy/https.conf").read_text()
        normal = proxy.render(template, {"PUBLIC_HOST": "176.53.246.245"})
        self.assertNotIn("mini-backend", normal)
        mini = proxy.render(template, {"PUBLIC_HOST": "176.53.246.245",
                                      "MINI_APP_PROXY_ENABLED": "true"})
        self.assertIn("location /api/v1/ { proxy_pass $mini_backend; }", mini)
        self.assertIn("location / { proxy_pass $mini_frontend; }", mini)
        self.assertIn("location /health/ { proxy_pass $core; }", mini)
        self.assertIn("location = /admin/ingestion/upload", mini)
        self.assertIn("location ^~ /internal/ { return 404; }", mini)
        self.assertNotIn("@APP_LOCATIONS@", mini)


class ReadinessTests(unittest.TestCase):
    def test_process_ready_signal_is_optional_and_owned_by_the_writing_pid(self):
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "ready.json"
            with patch.dict(os.environ, {"RAG_PROCESS_READY_FILE": str(marker)}):
                _process_status._ready()
                self.assertEqual(json.loads(marker.read_text())["pid"], os.getpid())
                marker.write_text(json.dumps({"pid": os.getpid() + 1}))
                _process_status._clear()
                self.assertTrue(marker.exists())
                _process_status._ready()
                _process_status._clear()
                self.assertFalse(marker.exists())

    def test_health_rejects_stale_supervisor_and_missing_worker_signal(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "status.json"
            status = {"ready": True, "updated": time.time(), "supervisor": 10,
                      "children": [11], "signals": ["worker-marker"]}
            path.write_text(json.dumps(status))
            with patch.object(server, "_status_path", return_value=path), \
                    patch.object(server, "_pid_alive", return_value=True), \
                    patch.object(server, "_role_ready", return_value=True) as ready, \
                    patch.object(server, "_http_ready", return_value=True):
                self.assertTrue(server.healthy("worker"))
                ready.return_value = False
                self.assertFalse(server.healthy("worker"))
                ready.return_value = True
                status["updated"] -= 60
                path.write_text(json.dumps(status))
                self.assertFalse(server.healthy("worker"))


@unittest.skipUnless(sys.platform == "linux", "Server supervision requires Linux")
class SupervisorTests(unittest.TestCase):
    def test_loss_of_any_required_process_stops_its_siblings(self):
        original_popen = subprocess.Popen
        children = []

        def spawn(*args, **kwargs):
            child = original_popen(*args, **kwargs)
            children.append(child)
            return child

        with tempfile.TemporaryDirectory() as directory:
            status = Path(directory) / "status.json"
            commands = {"http": [sys.executable, "-c", "import time; time.sleep(0.2)"],
                        "relay": [sys.executable, "-c", "import time; time.sleep(60)"]}
            with patch.object(server, "commands", return_value=commands), \
                    patch.object(server, "environment_values", return_value={}), \
                    patch.object(server, "_status_path", return_value=status), \
                    patch.object(server, "_role_ready", return_value=True), \
                    patch.object(server, "_http_ready", return_value=True), \
                    patch.object(server.signal, "signal"), \
                    patch.object(server.subprocess, "Popen", side_effect=spawn):
                self.assertEqual(server.run("core"), 1)
            self.assertFalse(status.exists())
            self.assertTrue(children and all(child.poll() is not None for child in children))


class ModelsTests(unittest.IsolatedAsyncioTestCase):
    async def _prepare(self, installed, download_revision="a" * 64, complete=True, item=None):
        calls = []
        catalog = dict(installed)

        def handler(request):
            calls.append((request.method, request.url.path))
            if request.url.path == "/api/tags":
                return httpx.Response(200, json={"models": [{"name": name, "digest": revision} for name, revision in catalog.items()]})
            if request.url.path == "/api/pull":
                name = json.loads(request.content)["model"]
                catalog[name] = download_revision
                body = json.dumps({"status": "success" if complete else "pulling manifest"}) + "\n"
                return httpx.Response(200, content=body)
            raise AssertionError("Unexpected request")

        original_client = httpx.AsyncClient
        client = original_client(base_url="http://ollama:11434", transport=httpx.MockTransport(handler))
        with patch.object(models, "RuntimeSettings", return_value=item or settings()), \
                patch.object(models, "environment_values", return_value={}), \
                patch.object(models.httpx, "AsyncClient", return_value=client), \
                patch.object(models, "start_models", new=AsyncMock()) as start, \
                patch.object(models, "close_models", new=AsyncMock()) as close:
            try:
                await models.prepare_models()
            finally:
                self.calls, self.start, self.close = calls, start, close

    async def test_existing_weights_are_not_downloaded_and_gateway_is_probed(self):
        await self._prepare({"embeddinggemma:300m": "a" * 64, "qwen3:0.6b": "a" * 64})
        self.assertFalse(any(path == "/api/pull" for _, path in self.calls))
        self.start.assert_awaited_once()
        self.close.assert_awaited_once()

    async def test_enabled_gigachat_probes_both_backends_without_downloading_api_model(self):
        with patch.dict(os.environ, {"GIGACHAT_MODEL": "test-model"}):
            item = settings()
            item.use_gigachat = True
            await self._prepare({"embeddinggemma:300m": "a" * 64, "qwen3:0.6b": "a" * 64}, item=item)
        self.assertFalse(any(path == "/api/pull" for _, path in self.calls))
        self.assertEqual(self.start.await_count, 2)
        first, second = [call.args[0] for call in self.start.await_args_list]
        self.assertEqual(first["generation"].provider, "ollama")
        self.assertEqual(set(second), {"generation"})
        self.assertEqual(second["generation"].provider, "gigachat")
        self.assertEqual(self.close.await_count, 2)

    async def test_same_model_used_by_multiple_roles_is_downloaded_once(self):
        await self._prepare({})
        self.assertEqual(sum(path == "/api/pull" for _, path in self.calls), 2)

    async def test_existing_mismatched_revision_is_not_silently_overwritten(self):
        with self.assertRaisesRegex(RuntimeError, "Installed model revision"):
            await self._prepare({"embeddinggemma:300m": "b" * 64})
        self.assertFalse(any(path == "/api/pull" for _, path in self.calls))
        self.start.assert_not_awaited()

    async def test_downloaded_mismatched_revision_blocks_application_start(self):
        with self.assertRaisesRegex(RuntimeError, "Downloaded model revision"):
            await self._prepare({}, download_revision="b" * 64)
        self.start.assert_not_awaited()

    async def test_interrupted_download_is_not_reported_as_success(self):
        with self.assertRaisesRegex(RuntimeError, "Incomplete Ollama"):
            await self._prepare({}, complete=False)
        self.start.assert_not_awaited()




COMPOSE = (["docker", "compose"] if shutil.which("docker") else
           ["docker-compose"] if shutil.which("docker-compose") else None)


@unittest.skipUnless(COMPOSE, "Docker Compose CLI unavailable")
class ComposeTests(unittest.TestCase):
    def test_default_server_services_and_network_exposure_without_real_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            env_file = Path(directory) / "test.env"
            env_file.write_text("PUBLIC_HOST=176.53.246.245\nPGPASSWORD=test-pg\nRABBITMQ_PASSWORD=test-rabbit\nS3_SECRET_KEY=test-s3\nGRAFANA_PASSWORD=test-grafana\n")
            # config --no-env-resolution validates the deployable graph without
            # reading or printing the real service .env. No daemon is required.
            command = [*COMPOSE, "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml"),
                       "config", "--no-env-resolution", "--format", "json"]
            environment = {key: value for key, value in os.environ.items()
                           if not key.startswith(("COMPOSE_", "RAG_", "PG", "RABBITMQ_", "S3_", "GRAFANA_", "PUBLIC_"))}
            result = subprocess.run(command, env=environment, capture_output=True, text=True, encoding="utf-8", timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            services = json.loads(result.stdout)["services"]
            self.assertTrue({"core", "worker", "models-init", "prepare", "migrate", "telegram-poller", "nginx", "grafana", "loki", "alloy"} <= services.keys())
            self.assertFalse(any(service.get("profiles") for service in services.values()))
            for name, service in services.items():
                for port in service.get("ports", []):
                    if port.get("host_ip") != "127.0.0.1":
                        self.assertEqual(name, "nginx")
                        self.assertIn(port["target"], (80, 443))
            self.assertEqual(services["core"]["depends_on"]["models-init"]["condition"], "service_completed_successfully")
            self.assertNotIn("webhook", services)
            self.assertNotIn("profiles", services["telegram-poller"])
            self.assertEqual(services["telegram-poller"]["depends_on"]["core"]["condition"], "service_healthy")
            self.assertEqual(services["analytics-init"]["depends_on"]["migrate"]["condition"], "service_completed_successfully")
            self.assertEqual(services["grafana"]["depends_on"]["analytics-init"]["condition"], "service_completed_successfully")
            self.assertEqual(services["grafana"]["environment"]["ANALYTICS_PASSWORD"], "test-grafana")
            self.assertEqual(services["analytics-init"]["environment"]["ANALYTICS_PASSWORD"], "test-grafana")
            self.assertEqual(services["analytics-init"]["environment"]["PGHOST"], "postgres")
            self.assertNotIn("PGPASSWORD", services["grafana"]["environment"])
            business_mounts = [volume for volume in services["grafana"]["volumes"] if "business" in volume["target"]]
            self.assertEqual(len(business_mounts), 3)
            self.assertTrue(all(volume["read_only"] for volume in business_mounts))
            spool = lambda name: {volume["source"] for volume in services[name]["volumes"] if volume["target"] == "/var/lib/rag/spool"}
            self.assertEqual(spool("core"), spool("worker"))
