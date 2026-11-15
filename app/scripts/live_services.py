r"""Start/recreate individual development services against the existing Podman stack.

From app/: .venv\Scripts\python.exe scripts/live_services.py relay results maintenance recovery
API uses port 8000; ingestion HTTP uses 8001. Stop the scaffold core before API.
Requires the existing infrastructure, network, spool volume and dependency image.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile

from rag._configuration import environment_values


ROOT = Path(__file__).resolve().parents[2]
COMMANDS = {
    "api": ["python", "-m", "uvicorn", "rag.rag_api.application:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"],
    "ingestion-http": ["python", "-m", "uvicorn", "rag.ingestion.application:app", "--host", "0.0.0.0", "--port", "8001", "--no-access-log"],
    "worker": ["python", "-m", "rag.runtime", "worker"],
    **{name: ["python", "-m", "rag.runtime", "serve", name]
       for name in ("relay", "recovery", "questions", "results", "maintenance")},
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("services", nargs="+", choices=COMMANDS)
    parser.add_argument("--image", default="localhost/rag-runtime-tests:local")
    parser.add_argument("--network", default="rag-hackathon_default")
    parser.add_argument("--spool-volume", default="rag-hackathon_worker_spool")
    args = parser.parse_args()
    values = environment_values()
    descriptor, filename = tempfile.mkstemp(prefix="rag-live-", suffix=".env")
    try:
        # Podman env files accept literal values, whereas our source uses dotenv quoting.
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            for key, value in values.items():
                # Only project settings are needed; do not copy arbitrary process secrets.
                if not key.startswith(("RAG_", "PG", "RABBITMQ_", "REDIS_", "QDRANT_", "S3_",
                                       "TELEGRAM_", "ADMIN_", "INGESTION_", "MODEL_GATEWAY_",
                                       "OLLAMA_", "WORKER_")):
                    continue
                if "\n" in value or "\r" in value:
                    raise ValueError("Multiline environment values are not supported")
                output.write(key + "=" + value + "\n")
        for service in args.services:
            name = "rag-live-" + service
            exists = subprocess.run(["podman", "container", "exists", name]).returncode
            if exists == 0:
                subprocess.run(["podman", "stop", name], check=True)
                subprocess.run(["podman", "rm", name], check=True)
            elif exists != 1:
                raise RuntimeError("Cannot inspect Podman containers")
            command = ["podman", "run", "-d", "--name", name, "--restart", "unless-stopped",
                       "--network", args.network, "--env-file", filename,
                       "-e", "PYTHONPATH=/workspace/app", "-v", f"{ROOT / 'app'}:/workspace/app:ro",
                       "-v", f"{args.spool_volume}:/var/lib/rag/spool", "-w", "/workspace/app",
                       "--no-healthcheck"]
            port = {"api": 8000, "ingestion-http": 8001}.get(service)
            if port:
                command.extend(["-p", f"127.0.0.1:{port}:{port}"])
            subprocess.run(command + [args.image] + COMMANDS[service], check=True)
            print(f"Started {name}; check startup with: podman logs {name}")
    finally:
        Path(filename).unlink(missing_ok=True)


if __name__ == "__main__":
    main()
