"""Verify the demo loader's HTTP contract and safe resume behavior."""

import argparse
import base64
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

import httpx

from scripts import load_demo_documents as script


class DemoUploadTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.directory = Path(temporary.name)
        for filename in script.FILENAMES:
            (self.directory / filename).write_bytes(b"%PDF-demo\n")
        self.args = argparse.Namespace(
            api_url="http://127.0.0.1:8000", ingestion_url="http://127.0.0.1:8001",
            downloads=self.directory, state=self.directory / "state.json",
        )
        self.config = dict(RAG_INGESTION_TOKEN="upload-secret", ADMIN_LOGIN="admin",
                           ADMIN_PASSWORD="password", TELEGRAM_WEBHOOK_URL="https://bot.example")
        self.calls = []

    def handle(self, request):
        self.calls.append(request)
        if request.method == "PUT":
            self.assertEqual(request.url.path, "/admin/users/verdenkaa")
            self.assertEqual(request.headers["Origin"], "https://bot.example")
            expected = base64.b64encode(b"admin:password").decode()
            self.assertEqual(request.headers["Authorization"], "Basic " + expected)
            self.assertEqual(json.loads(request.content), {"enabled": True})
            return httpx.Response(200, json={"username": "verdenkaa", "enabled": True})
        self.assertEqual(request.url.path, "/admin/ingestion/upload")
        self.assertEqual(request.headers["Authorization"], "Bearer upload-secret")
        self.assertEqual(request.headers["Content-Type"], "application/pdf")
        self.assertEqual(request.url.params["access_level"], "employees")
        self.assertIn(request.url.params["filename"], script.FILENAMES)
        self.assertEqual(request.content, b"%PDF-demo\n")
        # A crash after the server receives the body must leave an uncertain entry.
        state = json.loads(self.args.state.read_text(encoding="utf-8"))
        self.assertTrue(any(row["status"] == "unconfirmed" for row in state.values()))
        return httpx.Response(202, json={"kind": "accepted", "document_id": str(uuid4()),
                                         "job_id": str(uuid4())})

    def execute(self, handler=None):
        with httpx.Client(transport=httpx.MockTransport(handler or self.handle)) as client:
            with redirect_stdout(io.StringIO()):
                script.run(self.args, self.config, client)

    def test_http_contract_and_resume(self):
        self.execute()
        self.assertEqual(len(self.calls), 4)
        self.execute()
        self.assertEqual([request.method for request in self.calls], ["PUT", "POST", "POST", "POST", "PUT"])
        self.assertEqual(len(json.loads(self.args.state.read_text(encoding="utf-8"))), 3)

    def test_uncertain_post_is_not_retried(self):
        def timeout(request):
            if request.method == "PUT":
                return self.handle(request)
            raise httpx.ReadTimeout("server may have committed", request=request)

        with self.assertRaises(httpx.ReadTimeout):
            self.execute(timeout)
        with self.assertRaisesRegex(RuntimeError, "исход прошлого POST неизвестен"):
            self.execute()
        self.assertEqual(len(self.calls), 1)

    def test_explicit_new_revision_keeps_document_identity(self):
        self.execute()
        original = json.loads(self.args.state.read_text(encoding="utf-8"))
        by_name = {row["filename"]: row for row in original.values()}
        self.args.new_revision = True
        def handler(request):
            if request.method == "PUT":
                return self.handle(request)
            previous = by_name[request.url.params["filename"]]
            self.assertEqual(request.url.params["document_id"], previous["document_id"])
            return httpx.Response(202, json={"kind": "accepted", "document_id": previous["document_id"],
                                             "job_id": str(uuid4())})
        self.execute(handler)
        current = json.loads(self.args.state.read_text(encoding="utf-8"))
        for key, row in current.items():
            self.assertEqual(row["document_id"], original[key]["document_id"])
            self.assertEqual(row["previous_job_id"], original[key]["job_id"])
            self.assertNotEqual(row["job_id"], original[key]["job_id"])

    def test_missing_file_prevents_all_mutations(self):
        (self.directory / script.FILENAMES[-1]).unlink()
        with self.assertRaises(FileNotFoundError):
            self.execute()
        self.assertEqual(self.calls, [])

    def test_text_only_pdf_uploads_markdown_and_resumes_without_duplicates(self):
        self.args.text_only_pdf = True
        content = "# Test\n\nТекст документа.\n"
        uploaded = []
        def handler(request):
            if request.method == "PUT":
                return self.handle(request)
            self.assertEqual(request.headers["Content-Type"], "text/markdown")
            self.assertTrue(request.url.params["filename"].endswith(".md"))
            self.assertEqual(request.content, content.encode("utf-8"))
            uploaded.append(request.url.params["filename"])
            return httpx.Response(202, json={"kind": "accepted", "document_id": str(uuid4()),
                                             "job_id": str(uuid4())})
        with patch("rag.ingestion._parsers.PdfParser.parse", return_value=content):
            self.execute(handler)
            self.execute(handler)
        self.assertEqual(len(uploaded), 3)
        for filename in script.FILENAMES:
            self.assertEqual((self.directory / filename).read_bytes(), b"%PDF-demo\n")
