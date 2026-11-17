"""Start real core/worker and stores in an isolated Compose project with fake model transport.

Uses only synthetic credentials, does not start nginx and never contacts Telegram.
All test containers and volumes are removed on completion.
"""

import argparse
import json
import os
from pathlib import Path
import subprocess
import tempfile
import uuid


ROOT = Path(__file__).resolve().parents[2]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", default="rag-server-check")
    args = parser.parse_args()
    project = "rag-deploy-test-" + uuid.uuid4().hex[:10]
    bindings = {}
    for role in ("embedding", "generation", "injection"):
        bindings[role] = dict(profile_id=role + "-test", execution="remote", provider="ollama",
                              model_id="embeddinggemma:300m" if role == "embedding" else "qwen3:0.6b",
                              model_revision="sha256:" + "a" * 64, endpoint="http://ollama:11434",
                              credential_env=None, max_concurrency=1,
                              embedding_dimensions=768 if role == "embedding" else None)
    values = dict(PUBLIC_HOST="176.53.246.245", RAG_PUBLIC_URL="https://176.53.246.245",
                  TELEGRAM_BOT_TOKEN="123:synthetic", TELEGRAM_INTAKE_SECRET="synthetic",
                  PGUSER="rag", PGDATABASE="rag", PGPASSWORD="synthetic-postgres",
                  RABBITMQ_USER="rag", RABBITMQ_PASSWORD="synthetic-rabbit",
                  S3_ACCESS_KEY="rag", S3_SECRET_KEY="synthetic-s3-secret", S3_REGION="us-east-1",
                  ADMIN_LOGIN="admin", ADMIN_PASSWORD="synthetic-password", GRAFANA_PASSWORD="synthetic-grafana",
                  RAG_INGESTION_TOKEN="synthetic-ingestion", RAG_TEXT_ONLY="true", RAG_USE_RERANKER="false",
                  RAG_EMBEDDING_PROFILE="embedding-test", RAG_EMBEDDING_DIMENSIONS="768",
                  RAG_PREPARATION_PROFILE="ingestion-v1", INGESTION_PREPARATION_PROFILE="ingestion-v1",
                  INGESTION_EMBEDDING_PROFILE="embedding-test", INGESTION_GENERATION_PROFILE="generation-test",
                  INGESTION_EMBEDDING_DIMENSIONS="768", INGESTION_EMBEDDING_INPUT_CHARACTERS="1000",
                  INGESTION_TOPIC_INPUT_CHARACTERS="1000", INGESTION_GENERATION_MAX_OUTPUT_TOKENS="256",
                  INGESTION_CHUNK_CHARACTERS="500", INGESTION_CHUNK_OVERLAP="50",
                  MODEL_GATEWAY_PROVIDER_MODULES='["rag.model_gateway.ollama"]',
                  OLLAMA_GATEWAY_KEEP_ALIVE="-1", OLLAMA_GATEWAY_TEXT_CONTEXT_LENGTH="16384",
                  COMPOSE_PROFILES="",
                  RAG_TELEGRAM_BASE_URL="http://ollama:11434",
                  RAG_MODEL_BINDINGS=json.dumps(bindings), RAG_STARTUP_TIMEOUT_S="60",
                  MODEL_GATEWAY_STARTUP_TIMEOUT_S="60", API_WORKERS="1", INGESTION_HTTP_WORKERS="1")
    with tempfile.TemporaryDirectory(prefix="rag-deploy-test-") as directory:
        env_file = Path(directory) / "test.env"
        env_file.write_text("".join(key + "='" + value + "'\n" for key, value in values.items()), encoding="utf-8")
        override = Path(directory) / "override.json"
        override.write_text(json.dumps({"services": {"ollama": {
            "image": args.image, "entrypoint": ["python", "/test/fake_ollama.py"], "command": [],
            "cpus": 1, "mem_limit": "512m",
            "volumes": [{"type": "bind", "source": str(ROOT / "deploy/tests/fake_ollama.py"),
                         "target": "/test/fake_ollama.py", "read_only": True}],
            "healthcheck": {"test": ["CMD", "python", "-c", "from urllib.request import urlopen; urlopen('http://127.0.0.1:11434/api/tags').close()"]}
        }}}), encoding="utf-8")
        environment = {key: value for key, value in os.environ.items()
                       if not key.startswith(("COMPOSE_", "RAG_", "PG", "RABBITMQ_", "S3_", "GRAFANA_", "PUBLIC_", "OLLAMA_", "DEPLOY_"))}
        environment.update(DEPLOY_ENV_FILE=str(env_file), DEPLOY_APP_IMAGE=args.image)
        command = ["docker", "compose", "-p", project, "--env-file", str(env_file), "-f", str(ROOT / "compose.yaml"), "-f", str(override)]

        def run(*args, check=True, capture=False):
            return subprocess.run(command + list(args), env=environment, check=check,
                                  capture_output=capture, text=True, encoding="utf-8", errors="replace", timeout=300)

        try:
            run("up", "-d", "--no-build", "--wait", "--wait-timeout", "180", "core", "worker")
            run("exec", "-T", "core", "python", "-m", "deploy", "health", "core")
            run("exec", "-T", "worker", "python", "-m", "deploy", "health", "worker")
            # Ensure the production composition did not accidentally expose stores
            # or run public registration against the synthetic bot.
            result = run("ps", "--format", "json", capture=True)
            rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
            assert all(row["Service"] not in {"nginx", "webhook"} for row in rows)
            # Image EXPOSE metadata appears here too, with PublishedPort=0.
            assert not any(port.get("PublishedPort", 0) for row in rows for port in (row.get("Publishers") or []))
            # Exercise the real poller, API intake and PostgreSQL ledger. The
            # synthetic Telegram transport is served only inside this network.
            run("up", "-d", "--no-build", "--no-deps", "--wait", "--wait-timeout", "60", "telegram-poller")
            run("exec", "-T", "telegram-poller", "python", "-m", "deploy", "polling-health")
            inspect_ledger = '''import asyncio
from sqlalchemy import select
from rag.runtime.lifecycle import start_runtime, close_runtime
from rag.runtime.postgresql._db import _transaction, _one
from rag.runtime.postgresql._models import _TelegramUpdate
async def main():
    await start_runtime()
    try:
        async with _transaction() as session:
            row = await _one(session, select(_TelegramUpdate).where(_TelegramUpdate.update_id == 91743000))
            assert row is not None, 'Polled update was not durably recorded'
        print('Real polling-to-PostgreSQL intake passed')
    finally:
        await close_runtime()
asyncio.run(main())
'''
            run("exec", "-T", "core", "python", "-c", inspect_ledger)
            run("stop", "telegram-poller", "core", "worker")
            run("run", "--rm", "--no-deps", "-T", "-e", "RUN_RUNTIME_INTEGRATION=1",
                "-v", str(ROOT / "app/tests") + ":/opt/rag/tests:ro",
                "core", "python", "-m", "unittest", "discover", "-s", "tests",
                "-p", "test_runtime_integration.py", "-k", "empty_embedding", "-v")
            print("Isolated startup passed: migrations, storage, Model Gateway probes, API, all background roles, supervised worker and shared spool")
        except BaseException:
            run("logs", "--tail", "100", check=False)
            raise
        finally:
            # The random project prefix belongs exclusively to this invocation.
            assert project.startswith("rag-deploy-test-")
            run("down", "-v", "--remove-orphans", check=False)


if __name__ == "__main__":
    main()
