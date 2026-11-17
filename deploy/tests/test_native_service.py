"""Private HTTP behavior and long-input window coverage without downloading weights."""

from http.server import ThreadingHTTPServer
import json
from pathlib import Path
import runpy
import threading
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen


ROOT = Path(__file__).resolve().parents[2]
SERVER = runpy.run_path(str(ROOT / "deploy/native/server.py"))
BACKENDS = runpy.run_path(str(ROOT / "deploy/native/backends.py"))


class NativeServiceTests(unittest.TestCase):
    def test_windows_cover_tail_without_oversized_or_disjoint_chunks(self):
        for length in (0, 1, 254, 255, 510, 2000):
            with self.subTest(length=length):
                chunks = BACKENDS["windows"](list(range(length)))
                self.assertTrue(all(len(chunk) <= 254 for chunk in chunks))
                self.assertEqual(set(n for chunk in chunks for n in chunk), set(range(length)))
                if length:
                    self.assertEqual(chunks[-1][-1], length - 1)

    def test_authenticated_http_identity_and_profile_fence(self):
        class Backend:
            binding = dict(model_id="fixture/model", model_revision="a" * 40, profile_id="fixture-v1")
            calls = []

            def invoke(self, operation, payload):
                self.calls.append((operation, payload))
                return dict(profile_id="fixture-v1", score=.2, label=None)

        backend = Backend()
        server = ThreadingHTTPServer(("127.0.0.1", 0), SERVER["handler"]({"injection": backend}, "synthetic"))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.shutdown)
        base = "http://127.0.0.1:" + str(server.server_port)

        def request(path, value=None, authenticated=True):
            headers = {"Authorization": "Bearer synthetic"} if authenticated else {}
            req = Request(base + path, data=json.dumps(value).encode() if value is not None else None,
                          headers=headers)
            try:
                response = urlopen(req, timeout=2)
            except HTTPError as response:
                with response:
                    return response.code, json.loads(response.read())
            with response:
                return response.status, json.loads(response.read())

        self.assertEqual(request("/health/ready", authenticated=False)[0], 200)
        self.assertEqual(request("/v1/models/injection", authenticated=False)[0], 401)
        self.assertEqual(request("/v1/models/injection")[1], backend.binding)
        self.assertEqual(request("/injection")[0], 404)
        payload = dict(operation="classify_injection", profile_id="wrong", payload={"text": "private"})
        self.assertEqual(request("/v1/invoke/injection", payload)[0], 409)
        self.assertEqual(backend.calls, [])
        payload["profile_id"] = "fixture-v1"
        self.assertEqual(request("/v1/invoke/injection", payload)[1]["score"], .2)
        self.assertEqual(len(backend.calls), 1)
