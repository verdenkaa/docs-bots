"""Synthetic Ollama transport for the isolated startup test; never a production provider."""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json


class Handler(BaseHTTPRequestHandler):
    def respond(self, result):
        body = json.dumps(result).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.respond({"models": [{"name": name, "digest": "a" * 64}
                                  for name in ("embeddinggemma:300m", "qwen3:0.6b")]})

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if value.get("keep_alive") == "-1":
            self.send_error(400, "Bare numeric strings are not Go durations")
            return
        model = value["model"]
        if self.path == "/api/show":
            self.respond({"capabilities": ["embedding"] if model.startswith("embeddinggemma") else ["completion"]})
        elif self.path == "/api/embed":
            self.respond({"model": model, "embeddings": [[1.0] + [0.0] * 767 for _ in value["input"]]})
        elif self.path == "/api/chat":
            content = json.dumps({"label": "benign"}) if isinstance(value.get("format"), dict) else "OK"
            self.respond({"model": model, "done": True, "done_reason": "stop",
                          "message": {"role": "assistant", "content": content},
                          "prompt_eval_count": 5, "eval_count": 2})
        else:
            self.send_error(404)

    def log_message(self, *args):
        pass


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 11434), Handler).serve_forever()
