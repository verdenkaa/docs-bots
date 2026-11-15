"""Mini App authentication and HTTP boundary checks, without infrastructure."""

import hashlib
import hmac
import json
import os
import time
import unittest
from urllib.parse import urlencode
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import httpx
from fastapi import FastAPI, HTTPException
from pydantic import ValidationError
from rag.rag_api import mini_app
from rag.rag_api import _mini_app_auth as auth
from rag.rag_api.mini_app_contracts import WebQuestionInput

TOKEN = "12345:unit_test_token"


def signed(user_id=7, username="Alice", timestamp=None, **extra):
    values = {
        "auth_date": str(int(time.time()) if timestamp is None else timestamp),
        "user": json.dumps(
            {"id": user_id, "username": username}, separators=(",", ":")
        ),
        "query_id": "unit-query",
        **extra,
    }
    secret = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    values["hash"] = hmac.new(
        secret,
        "\n".join(f"{k}={v}" for k, v in sorted(values.items())).encode(),
        hashlib.sha256,
    ).hexdigest()
    return urlencode(values)


class MiniAppAuthTests(unittest.TestCase):
    def test_verified_username_and_separate_admin_ids(self):
        user = auth._verify(signed(), TOKEN, 3600, set())
        self.assertEqual((user.user_id, user.username, user.role), (7, "alice", "user"))
        self.assertEqual(auth._verify(signed(), TOKEN, 3600, {7}).role, "admin")

    def test_tampering_duplicates_missing_user_and_wrong_token(self):
        for raw, token in [
            (signed().replace("Alice", "Admin"), TOKEN),
            (signed() + "&user=%7B%22id%22%3A9%7D", TOKEN),
            (signed(user="{}"), TOKEN),
            (signed(), "wrong"),
            ("hash=x", TOKEN),
        ]:
            with self.subTest(raw=raw[:12]):
                with self.assertRaises(HTTPException) as raised:
                    auth._verify(raw, token, 3600, {7})
                self.assertEqual(raised.exception.status_code, 401)

    def test_expired_and_future_data(self):
        for timestamp in (100, 5000):
            with self.assertRaises(HTTPException) as raised:
                auth._verify(signed(timestamp=timestamp), TOKEN, 3600, set(), now=4000)
            self.assertEqual(raised.exception.status_code, 401)

    def test_bad_id_and_username(self):
        for kwargs in (
            {"user_id": True},
            {"user_id": -1},
            {"user_id": 2**63},
            {"username": "../admin"},
        ):
            with self.assertRaises(HTTPException):
                auth._verify(signed(**kwargs), TOKEN, 3600, set())

    def test_text_only_and_no_client_identity(self):
        self.assertEqual(WebQuestionInput(text="  question  ").text, "question")
        for value in (
            {"text": " "},
            {"text": "Q", "role": "admin"},
            {"text": "Q", "upload_id": str(uuid4())},
            {"text": "Q", "user_id": 8},
            {"text": "Q", "username": "admin"},
        ):
            with self.assertRaises(ValidationError):
                WebQuestionInput.model_validate(value)

    def test_rate_limit_is_bounded_and_resets(self):
        auth._windows.clear()
        with patch.object(auth.time, "monotonic", return_value=100):
            for _ in range(120):
                auth._rate_limit(7)
            with self.assertRaises(HTTPException) as raised:
                auth._rate_limit(7)
            self.assertEqual(raised.exception.status_code, 429)
        with patch.object(auth.time, "monotonic", return_value=161):
            auth._rate_limit(7)
        auth._windows.clear()


class MiniAppHttpTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.env = patch.dict(
            os.environ,
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_ADMIN_IDS": "99",
                "MINI_APP_URL": "https://mini.example.org",
                "TELEGRAM_BOT_TOKEN": TOKEN,
                "TELEGRAM_WEBHOOK_SECRET": "test-secret",
                "TELEGRAM_WEBHOOK_URL": "https://rag.example.org",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        auth._windows.clear()
        self.app = FastAPI()
        self.app.include_router(mini_app.router)
        self.client = httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app),
            base_url="https://rag.example.org",
        )
        self.addAsyncCleanup(self.client.aclose)
        self.headers = {"Authorization": "tma " + signed()}

    async def test_unsigned_requests_rejected(self):
        for header in (
            {},
            {"Authorization": "tma username=Alice"},
            {"Authorization": "Basic abc"},
        ):
            response = await self.client.get("/api/v1/me", headers=header)
            self.assertEqual(response.status_code, 401)

    async def test_role_is_from_verified_identity(self):
        async def me(identity):
            return identity.model_dump()

        with patch.object(mini_app.storage, "_me", side_effect=me):
            self.assertEqual(
                (await self.client.get("/api/v1/me", headers=self.headers)).json()[
                    "role"
                ],
                "user",
            )
            admin = {"Authorization": "tma " + signed(user_id=99)}
            self.assertEqual(
                (await self.client.get("/api/v1/me", headers=admin)).json()["role"],
                "admin",
            )

    async def test_all_admin_routes_reject_user_before_storage(self):
        case = str(uuid4())
        for method, path in [
            ("GET", "/admin/tickets"),
            ("GET", "/admin/tickets/" + case + "/messages"),
            ("POST", "/admin/tickets/" + case + "/messages"),
            ("POST", "/admin/tickets/" + case + "/close"),
            ("GET", "/admin/users"),
            ("PUT", "/admin/users/alice"),
            (
                "GET",
                "/admin/metrics?from_at=2026-01-01T00:00:00Z&to_at=2027-01-01T00:00:00Z",
            ),
        ]:
            with self.subTest(path=path):
                response = await self.client.request(
                    method, "/api/v1" + path, headers=self.headers
                )
                self.assertEqual(response.status_code, 403)

    async def test_question_requires_key_and_rejects_images_or_role(self):
        with patch.object(mini_app.storage, "_register", new=AsyncMock()) as register:
            self.assertEqual(
                (
                    await self.client.post(
                        "/api/v1/questions", headers=self.headers, json={"text": "Q"}
                    )
                ).status_code,
                422,
            )
            headers = {**self.headers, "Idempotency-Key": "key-123456"}
            for extra in (
                {"image": "data"},
                {"role": "admin"},
                {"username": "bob"},
                {"user_id": 99},
            ):
                response = await self.client.post(
                    "/api/v1/questions", headers=headers, json={"text": "Q", **extra}
                )
                self.assertEqual(response.status_code, 422)
            register.assert_not_awaited()

    async def test_post_returns_202_without_running_core(self):
        with patch.object(
            mini_app.storage,
            "_register",
            new=AsyncMock(return_value={"request_id": 17}),
        ) as register:
            response = await self.client.post(
                "/api/v1/questions",
                headers={**self.headers, "Idempotency-Key": "key-123456"},
                json={"text": "Q"},
            )
        self.assertEqual(response.status_code, 202)
        self.assertEqual(response.json(), {"request_id": 17})
        self.assertEqual(register.call_args.args[0].user_id, 7)

    async def test_disabled_upload_never_registers_anything(self):
        with patch.object(mini_app.storage, "_register", new=AsyncMock()) as register:
            response = await self.client.post(
                "/api/v1/uploads", headers=self.headers, content=b"not-an-image" * 10000
            )
        self.assertEqual(response.status_code, 501)
        register.assert_not_awaited()

    async def test_body_cap_and_no_list_questions(self):
        headers = {**self.headers, "Idempotency-Key": "key-123456"}
        response = await self.client.post(
            "/api/v1/questions", headers=headers, content=b"x" * 32769
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(
            (
                await self.client.get("/api/v1/questions", headers=self.headers)
            ).status_code,
            405,
        )

    async def test_webhook_redirect_preserves_callbacks_and_legacy_switch(self):
        from rag.rag_api._mini_app_webhook import _redirect

        update = {
            "update_id": 100,
            "message": {
                "from": {"id": 7, "is_bot": False},
                "chat": {"id": 7, "type": "private"},
                "text": "/start",
            },
        }
        with patch(
            "rag.rag_api._mini_app_webhook.record_update", new=AsyncMock()
        ) as record:
            result = await _redirect(update)
            self.assertEqual(
                json.loads(result.body)["reply_markup"]["inline_keyboard"][0][0][
                    "web_app"
                ]["url"],
                "https://mini.example.org",
            )
            record.assert_awaited_once()
            self.assertIsNone(await _redirect({"callback_query": {}, **update}))
            with patch.dict(os.environ, {"MINI_APP_ENABLED": "false"}):
                self.assertIsNone(await _redirect(update))


if __name__ == "__main__":
    unittest.main()
