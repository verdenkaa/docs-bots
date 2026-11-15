"""Private model server; imports do not download weights or start HTTP."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import hmac
import json
import os
from pathlib import Path
import threading
import sys


def load_backends(bindings, manifest):
    from backends import Backend
    result = {}
    for role, binding in bindings.items():
        if binding["provider"] != "native_cpu":
            continue
        expected = manifest[role]
        if any(binding[key] != expected[key] for key in ("provider", "model_id", "model_revision", "profile_id")):
            raise ValueError("Native model revision does not match the locked manifest: " + role)
        print("Preparing native model: " + role, flush=True)
        backend = Backend(role, binding, expected)
        backend.load()
        result[role] = backend
        print("Native weights loaded: " + role, flush=True)
    return result


def handler(backends, token):
    locks = {role: threading.Lock() for role in backends}

    class Handler(BaseHTTPRequestHandler):
        def send(self, status, value):
            body = json.dumps(value, allow_nan=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def authorized(self):
            value = self.headers.get("Authorization", "")
            return token and hmac.compare_digest(value.encode(), ("Bearer " + token).encode())

        def do_GET(self):
            if self.path == "/health/ready":
                self.send(200, {"ready": True})
                return
            if not self.authorized():
                self.send(401, {"error": "unauthorized"})
                return
            role = self.path[len("/v1/models/"):] if self.path.startswith("/v1/models/") else ""
            backend = backends.get(role)
            if not backend:
                self.send(404, {"error": "unknown_model"})
                return
            self.send(200, {key: backend.binding[key] for key in ("model_id", "model_revision", "profile_id")})

        def do_POST(self):
            if not self.authorized():
                self.send(401, {"error": "unauthorized"})
                return
            role = self.path[len("/v1/invoke/"):] if self.path.startswith("/v1/invoke/") else ""
            backend = backends.get(role)
            if not backend:
                self.send(404, {"error": "unknown_model"})
                return
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= 32 * 1024 * 1024:
                    self.send(413, {"error": "invalid_input_size"})
                    return
                value = json.loads(self.rfile.read(size))
                if value["profile_id"] != backend.binding["profile_id"]:
                    self.send(409, {"error": "model_profile_mismatch"})
                    return
                with locks[role]:
                    result = backend.invoke(value["operation"], value["payload"])
                self.send(200, result)
            except (ValueError, KeyError, TypeError):
                self.send(400, {"error": "model_invalid_input"})
            except Exception as error:
                print("Native inference failed: " + role + ": " + type(error).__name__, file=sys.stderr, flush=True)
                self.send(503, {"error": "model_unavailable"})

        def log_message(self, *args):
            pass

    return Handler


def main():
    bindings = json.loads(os.environ.get("RAG_MODEL_BINDINGS", "{}"))
    token = os.environ.get("NATIVE_MODEL_TOKEN", "")
    if not token:
        raise ValueError("NATIVE_MODEL_TOKEN must be configured")
    manifest = json.loads((Path(__file__).parent / "profile.json").read_text())
    backends = load_backends(bindings, manifest)
    ThreadingHTTPServer(("0.0.0.0", 8090), handler(backends, token)).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("Native model startup failed: " + type(error).__name__, file=sys.stderr, flush=True)
        sys.exit(1)
