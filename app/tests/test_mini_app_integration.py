"""Real PostgreSQL web-channel lifecycle; no working schema is modified.

Set RUN_MINI_APP_INTEGRATION=1 and PG* for an isolated test database.
Model inference quality and Telegram UI require a separate full deployment.
"""

import asyncio
import hashlib
import hmac
import json
import os
import time
import unittest
from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4
from urllib.parse import urlencode
import httpx

from fastapi import HTTPException
from sqlalchemy import func, insert, select, update
from sqlalchemy.schema import DropSchema
from alembic import command
from rag.contracts import (
    AnsweredOutcome,
    NoticeOutcome,
    SupportOutcome,
    RagResult,
    SourceRef,
    ImageRef,
)
from rag.rag_api.contracts import (
    Conflict,
    NotFound,
    TicketMessageInput,
    AccessRecord,
    Principal,
    NewQuestion,
)
from rag.rag_api.mini_app_contracts import MiniAppIdentity, WebQuestionInput
from rag.runtime import _state
from rag.runtime._config import RuntimeSettings
from rag.runtime.lifecycle import migrate_runtime, _engine, _migration_config
from rag.runtime.postgresql import _mini_app as web, rag_core, rag_api, support, outbox
from rag.runtime.postgresql._db import _transaction, _execute
from rag.runtime.postgresql._models import (
    _Employee,
    _Request,
    _WebRequest,
    _Delivery,
    _Outbox,
    _SupportCase,
)


@unittest.skipUnless(
    os.environ.get("RUN_MINI_APP_INTEGRATION") == "1",
    "Set RUN_MINI_APP_INTEGRATION=1 and PG*",
)
class MiniAppPostgresTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.schema = "mini_app_test_" + uuid4().hex
        self.env = patch.dict(
            os.environ,
            {
                "RAG_PG_SCHEMA": self.schema,
                "ADMIN_LOGIN": "test-admin",
                "ADMIN_PASSWORD": "test-password",
            },
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        await migrate_runtime()
        self.settings = RuntimeSettings()
        self.resources = _state._Resources(self.settings)
        self.resources.engine = _engine(self.settings)
        _state._set(self.resources)
        self.addAsyncCleanup(self._cleanup)
        self.user = MiniAppIdentity(user_id=70001, username="alice")
        self.other = MiniAppIdentity(user_id=70002, username="bob")
        self.admin = MiniAppIdentity(user_id=70003, username="operator", role="admin")
        async with _transaction() as session:
            for username in ("alice", "bob"):
                await _execute(
                    session, insert(_Employee).values(username=username, enabled=True)
                )

    async def _cleanup(self):
        _state._set(None)
        self.assertTrue(
            self.schema.startswith("mini_app_test_") and len(self.schema) == 46
        )
        async with self.resources.engine.begin() as connection:
            await connection.execute(DropSchema(self.schema, cascade=True))
        await self.resources.engine.dispose()

    async def register(self, key=None, text="How?"):
        return (
            await web._register(
                self.user, key or uuid4().hex, WebQuestionInput(text=text)
            )
        )["request_id"]

    async def complete(self, request_id, outcome=None):
        claimed = await rag_core.claim_question(request_id)
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.input.kind, "text")
        outcome = outcome or NoticeOutcome(
            kind="notice", code="technical_error", text="Temporary error"
        )
        self.assertTrue(await rag_core.complete_question(claimed.attempt, outcome))
        self.assertFalse(await rag_core.complete_question(claimed.attempt, outcome))
        return claimed

    def answer(self, images=False):
        image_id = uuid4()
        result = RagResult(
            answer_text="**Answer**"
            + (f" ![Diagram](image://{image_id})" if images else ""),
            sources=[
                SourceRef(
                    chunk_id=uuid4(),
                    document_id=uuid4(),
                    revision_id=uuid4(),
                    source_name="Guide",
                )
            ],
            images=[ImageRef(image_id=image_id, description="Diagram")]
            if images
            else [],
        )
        return AnsweredOutcome(kind="answered", result=result)

    async def test_migration_matches_metadata(self):
        async with self.resources.engine.begin() as connection:

            def check(sync):
                config = _migration_config()
                config.attributes.update(connection=sync, schema=self.schema)
                command.check(config)

            await connection.run_sync(check)

    async def test_real_http_auth_admission_results_support_metrics_and_legacy_auth(
        self,
    ):
        from rag.rag_api.application import app

        token = "12345:integration_fixture_token"

        def headers(identity):
            values = {
                "auth_date": str(int(time.time())),
                "user": json.dumps(
                    {"id": identity.user_id, "username": identity.username}
                ),
            }
            secret = hmac.digest(b"WebAppData", token.encode(), "sha256")
            values["hash"] = hmac.new(
                secret,
                "\n".join(f"{k}={v}" for k, v in sorted(values.items())).encode(),
                hashlib.sha256,
            ).hexdigest()
            return {"Authorization": "tma " + urlencode(values)}

        with patch.dict(
            os.environ,
            {
                "MINI_APP_ENABLED": "true",
                "MINI_APP_ADMIN_IDS": str(self.admin.user_id),
                "MINI_APP_URL": "https://mini.example.org",
                "TELEGRAM_BOT_TOKEN": token,
                "TELEGRAM_WEBHOOK_SECRET": "test-secret",
                "TELEGRAM_WEBHOOK_URL": "https://rag.example.org",
            },
        ):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="https://rag.example.org",
            ) as client:
                user, other, admin = (
                    headers(self.user),
                    headers(self.other),
                    headers(self.admin),
                )
                self.assertEqual((await client.get("/api/v1/me")).status_code, 401)
                self.assertTrue(
                    (await client.get("/api/v1/me", headers=user)).json()["admitted"]
                )
                key = uuid4().hex
                created = await client.post(
                    "/api/v1/questions",
                    headers={**user, "Idempotency-Key": key},
                    json={"text": "HTTP question"},
                )
                self.assertEqual(created.status_code, 202)
                request_id = created.json()["request_id"]
                repeated = await client.post(
                    "/api/v1/questions",
                    headers={**user, "Idempotency-Key": key},
                    json={"text": "HTTP question"},
                )
                self.assertEqual(repeated.json()["request_id"], request_id)
                self.assertEqual(
                    (
                        await client.get(
                            f"/api/v1/questions/{request_id}", headers=other
                        )
                    ).status_code,
                    404,
                )
                await self.complete(request_id, self.answer())
                self.assertEqual(
                    (
                        await client.get(
                            f"/api/v1/questions/{request_id}", headers=user
                        )
                    ).json()["outcome"]["result"]["answer_text"],
                    "**Answer**",
                )
                case = await client.post(
                    f"/api/v1/questions/{request_id}/support",
                    headers=user,
                    json={"reason": "dissatisfied"},
                )
                self.assertEqual(case.status_code, 200)
                self.assertEqual(
                    len(
                        (await client.get("/api/v1/support", headers=user)).json()[
                            "items"
                        ]
                    ),
                    1,
                )
                self.assertEqual(
                    (await client.get("/api/v1/support", headers=other)).json()[
                        "items"
                    ],
                    [],
                )
                query = {
                    "from_at": (datetime.now(UTC) - timedelta(hours=1)).isoformat(),
                    "to_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
                }
                result = await client.get(
                    "/api/v1/admin/metrics", headers=admin, params=query
                )
                self.assertEqual(result.status_code, 200, result.text)
                facts = result.json()
                self.assertEqual(
                    (facts["requests"], facts["answered"], facts["dissatisfied"]),
                    (1, 1, 1),
                )
                self.assertEqual(facts["reply_duration_ms"], [])
                self.assertEqual(len(facts["web_result_duration_ms"]), 1)
                self.assertEqual(
                    (
                        await client.get("/api/v1/admin/tickets", headers=user)
                    ).status_code,
                    403,
                )
                self.assertEqual(
                    (await client.get("/tickets", headers=admin)).status_code, 401
                )
                self.assertEqual(
                    (
                        await client.get(
                            "/tickets", auth=("test-admin", "test-password")
                        )
                    ).status_code,
                    200,
                )

    async def test_atomic_idempotency_conflicts_and_shared_slot(self):
        key = uuid4().hex
        first = await self.register(key)
        self.assertEqual(await self.register(key), first)
        with self.assertRaises(Conflict):
            await self.register(key, "Changed")
        with self.assertRaises(HTTPException) as raised:
            await self.register()
        self.assertEqual(raised.exception.status_code, 409)
        from rag.contracts import TextQuestion

        legacy = await rag_api.register_question(
            NewQuestion(
                update_id=991,
                user_id=self.user.user_id,
                chat_id=self.user.user_id,
                username="alice",
                received_at=datetime.now(UTC),
                input=TextQuestion(kind="text", text="Legacy"),
            )
        )
        self.assertEqual(legacy.kind, "busy")
        async with _transaction() as session:
            row = (
                await _execute(
                    session, select(_Request).where(_Request.request_id == first)
                )
            ).scalar_one()
            self.assertIsNone(row.chat_id)
            self.assertIsNone(row.update_id)
            self.assertEqual(
                (
                    await _execute(
                        session,
                        select(func.count())
                        .select_from(_Outbox)
                        .where(_Outbox.route == "rag.questions"),
                    )
                ).scalar_one(),
                1,
            )

    async def test_concurrent_admission_and_completion_without_view(self):
        values = await asyncio.gather(
            self.register(), self.register(), return_exceptions=True
        )
        accepted = [value for value in values if isinstance(value, int)]
        self.assertEqual(len(accepted), 1)
        await self.complete(accepted[0], self.answer())
        self.assertIsInstance(await self.register(), int)
        async with _transaction() as session:
            self.assertEqual(
                (
                    await _execute(session, select(func.count()).select_from(_Delivery))
                ).scalar_one(),
                0,
            )
            self.assertIsNotNone(
                (
                    await _execute(
                        session,
                        select(_WebRequest.completed_at).where(
                            _WebRequest.request_id == accepted[0]
                        ),
                    )
                ).scalar_one()
            )

    async def test_owner_scopes_and_images(self):
        request_id = await self.register()
        answer = self.answer(images=True)
        await self.complete(request_id, answer)
        with self.assertRaises(NotFound):
            await web._question(self.other, request_id)
        image_id = answer.result.images[0].image_id
        await web._image_allowed(self.user, image_id)
        with self.assertRaises(NotFound):
            await web._image_allowed(self.other, image_id)
        with self.assertRaises(NotFound):
            await web._image_allowed(self.user, uuid4())

    async def test_recovery_restart_and_revoked_admission(self):
        key = uuid4().hex
        request_id = await self.register(key)
        async with _transaction() as session:
            await _execute(
                session,
                update(_Outbox).values(
                    published_at=datetime.now(UTC) - timedelta(hours=1)
                ),
            )
        self.assertEqual(await outbox.recover_pending_work("rag.questions", 10), 1)
        self.assertEqual(await outbox.recover_pending_work("rag.questions", 10), 0)
        self.assertEqual(
            await rag_api.queue_waiting_statuses(
                datetime.now(UTC) + timedelta(hours=1), 10
            ),
            0,
        )
        await web._set_access(self.admin, "alice", False)
        self.assertEqual(await self.register(key), request_id)
        await self.complete(request_id)
        await self.resources.engine.dispose()
        self.resources.engine = _engine(self.settings)
        self.assertEqual(
            (await web._question(self.user, request_id))["outcome"]["kind"], "notice"
        )
        with self.assertRaises(HTTPException) as raised:
            await self.register()
        self.assertEqual(raised.exception.status_code, 403)

    async def test_automatic_support_and_legacy_public_read(self):
        request_id = await self.register()
        await self.complete(
            request_id, SupportOutcome(kind="support", reason="no_evidence")
        )
        case = (await web._question(self.user, request_id))["support"]
        self.assertEqual(case.state, "creating")
        opened = await support.create_support_ticket(
            case.case_id, "https://rag.example.org/tickets/" + str(case.case_id)
        )
        self.assertEqual(opened.state, "open")
        self.assertEqual(len((await web._tickets(self.user, 30, None))["items"]), 1)
        self.assertEqual((await web._tickets(self.other, 30, None))["items"], [])
        self.assertEqual(
            (await support.read_ticket(case.case_id)).history[0].outcome, "support"
        )
        await self.register()  # Automatic ticket does not retain the question slot.
        async with _transaction() as session:
            self.assertEqual(
                (
                    await _execute(session, select(func.count()).select_from(_Delivery))
                ).scalar_one(),
                0,
            )

    async def test_manual_timer_idempotency_messages_and_close(self):
        request_id = await self.register()
        await self.complete(request_id, self.answer())
        case = await web._start_support(self.user, request_id, "dissatisfied")
        duplicate = await web._start_support(self.user, request_id, "dissatisfied")
        self.assertEqual(
            (case.case_id, case.check_at), (duplicate.case_id, duplicate.check_at)
        )
        self.assertEqual(
            (await web._pending_support(self.user, 30, None))["items"][0].case_id,
            case.case_id,
        )
        self.assertEqual(
            (await web._pending_support(self.other, 30, None))["items"], []
        )
        with self.assertRaises(Conflict):
            await web._support_case(self.user, case.case_id, "confirm")
        with self.assertRaises(NotFound):
            await web._support_case(self.other, case.case_id)
        async with _transaction() as session:
            await _execute(
                session,
                update(_SupportCase)
                .where(_SupportCase.case_id == case.case_id)
                .values(check_at=datetime.now(UTC) - timedelta(seconds=1)),
            )
        self.assertTrue(
            await support.queue_support_confirmation(case.case_id, datetime.now(UTC))
        )
        confirmed = await web._support_case(self.user, case.case_id, "confirm")
        self.assertEqual(confirmed.state, "creating")
        await support.create_support_ticket(
            case.case_id, "https://rag.example.org/tickets/" + str(case.case_id)
        )
        body = TicketMessageInput(client_message_id=uuid4(), text="Details")
        saved = await web._append_message(self.user, case.case_id, body)
        self.assertEqual(
            (await web._append_message(self.user, case.case_id, body)).message_id,
            saved.message_id,
        )
        with self.assertRaises(Conflict):
            await web._append_message(
                self.user, case.case_id, body.model_copy(update={"text": "Changed"})
            )
        with self.assertRaises(NotFound):
            await web._append_message(self.other, case.case_id, body)
        operator = await web._append_message(
            self.admin,
            case.case_id,
            TicketMessageInput(client_message_id=uuid4(), text="Resolved"),
            admin=True,
        )
        self.assertEqual(
            (operator.author_role, operator.author_login),
            ("operator", "telegram:70003"),
        )
        view = await support.read_ticket(case.case_id)
        self.assertEqual(view.history[0].answer_text, "**Answer**")
        self.assertEqual(len(view.messages), 2)
        closed = await web._close_ticket(self.admin, case.case_id)
        self.assertEqual(closed.state, "closed")
        self.assertEqual(
            (await web._append_message(self.user, case.case_id, body)).message_id,
            saved.message_id,
        )
        with self.assertRaises(Conflict):
            await web._append_message(
                self.user,
                case.case_id,
                TicketMessageInput(client_message_id=uuid4(), text="New"),
            )
        async with _transaction() as session:
            self.assertEqual(
                (
                    await _execute(session, select(func.count()).select_from(_Delivery))
                ).scalar_one(),
                0,
            )

    async def test_decline_and_basic_admin_remain_separate(self):
        request_id = await self.register()
        await self.complete(request_id, self.answer())
        case = await web._start_support(self.user, request_id, "human_request")
        async with _transaction() as session:
            await _execute(
                session,
                update(_SupportCase)
                .where(_SupportCase.case_id == case.case_id)
                .values(check_at=datetime.now(UTC) - timedelta(seconds=1)),
            )
        self.assertEqual(
            (await web._support_case(self.user, case.case_id, "decline")).state,
            "cancelled",
        )
        self.assertEqual(
            (await web._support_case(self.user, case.case_id, "decline")).state,
            "cancelled",
        )
        await support.write_user_access(
            Principal(login="test-admin"), AccessRecord(username="legacy", enabled=True)
        )
        self.assertTrue((await support.read_user_access("legacy")).enabled)
        from rag.rag_api.contracts import AccessDenied

        with self.assertRaises(AccessDenied):
            await support.write_user_access(
                Principal(login="telegram:70003"),
                AccessRecord(username="legacy", enabled=False),
            )
