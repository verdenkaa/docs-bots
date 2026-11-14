"""Негативные проверки обязательных полей, плейсхолдеров и Telegram-форматирования."""

from datetime import timedelta
import unittest
from uuid import uuid4

from pydantic import TypeAdapter, ValidationError

from contract_examples import IMAGE, MARKDOWN, NOW, source
from rag.contracts import AnsweredOutcome, DeliveryAttempt, GeneratedAnswer, ImageRef, QuestionInput, RagResult, RequestRef
from rag.ingestion.contracts import ConvertedDocument, UploadSource
from rag.model_gateway.contracts import InjectionResult
from rag.rag_api.contracts import (
    AccessRecord, Principal, SupportCommand, TicketMessage, ClaimedDelivery, InlineAction, IntakeResult, MetricsSnapshot,
    SendResult, SupportAction, SupportCase, TelegramEntity, TextPart,
)
from rag.runtime.rabbitmq import OutboxNotice


class DtoValidationTests(unittest.TestCase):
    def test_question_and_route_boundaries(self):
        for raw in (
            '{"request_id":"1"}', '{"request_id":true}', '{"request_id":0}',
            '{"request_id":1,"extra":0}',
        ):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                RequestRef.model_validate_json(raw)
        for raw in ('{"kind":"image"}', '{"kind":"text","text":"  "}', '{"kind":"unknown"}'):
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                TypeAdapter(QuestionInput).validate_json(raw)
        with self.assertRaises(ValidationError):
            TypeAdapter(OutboxNotice).validate_python({
                "message_id": uuid4(), "route": "rag.results", "event": {"request_id": 1},
            })
        with self.assertRaises(ValidationError):
            TypeAdapter(SendResult).validate_json('{"kind":"sent"}')
        with self.assertRaises(ValidationError):
            UploadSource(filename="guide.docx", access_level="employees", source_url="https://old.example")

    def test_intake_and_support_actions(self):
        for values in ({"kind": "accepted"}, {"kind": "busy", "request_id": 1}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                IntakeResult(**values)
        for values in (
            {"kind": "offer"}, {"kind": "start"}, {"kind": "confirm"}, {"kind": "decline"},
            {"kind": "start", "request_id": 1, "reason": "human_request"},
        ):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                SupportAction(**values)
        SupportAction(kind="offer", reason="human_request")
        SupportAction(kind="start", request_id=1)
        SupportAction(kind="confirm", case_id=uuid4())

    def test_callback_origin_and_public_ticket_author(self):
        base = dict(update_id=1, user_id=7, chat_id=7, received_at=NOW, username="employee_name")
        start = SupportAction(kind="start", request_id=1)
        for fields in (
            dict(origin="callback", action=start),
            dict(origin="text", action=start),
            dict(origin="text", action=SupportAction(kind="offer", reason="human_request"), callback_message_id=77),
            dict(origin="callback", callback_message_id=77, action=SupportAction(kind="offer", reason="dissatisfied")),
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                SupportCommand(**base, **fields)
        SupportCommand(**base, origin="text", action=SupportAction(kind="offer", reason="human_request"))
        for username in ("@employee", "Employee", "", "привет", "a" * 33):
            with self.subTest(username=username), self.assertRaises(ValidationError):
                AccessRecord(username=username, enabled=True)
        with self.assertRaises(ValidationError):
            Principal(user_id=7, role="admin")
        message = dict(message_id=1, case_id=uuid4(), text="Сообщение", created_at=NOW)
        for author in (dict(author_role="operator"), dict(author_role="visitor", author_login="admin"),
                       dict(author_role="visitor", author_id=7)):
            with self.subTest(author=author), self.assertRaises(ValidationError):
                TicketMessage(**message, **author)

    def test_ticket_stages(self):
        base = dict(case_id=uuid4(), request_id=1, user_id=7, reason="no_evidence")
        for fields in (
            {"state": "open"}, {"state": "closed", "created_at": NOW, "ticket_url": "https://bot/ticket"},
            {"state": "waiting"}, {"state": "creating", "ticket_url": "https://bot/ticket"},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                SupportCase(**base, **fields)
        SupportCase(**base, state="open", created_at=NOW, ticket_url="https://bot/ticket")
        with self.assertRaises(ValidationError):
            MetricsSnapshot(from_at=NOW, to_at=NOW - timedelta(seconds=1), requests=0,
                            answered=0, escalated=0, dissatisfied=0,
                            reply_duration_ms=[], ticket_duration_ms=[])

    def test_buttons_and_utf16_entities(self):
        for values in ({}, {"callback_data": "x", "url": "https://bot"},
                       {"callback_data": "я" * 33}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                InlineAction(text="Кнопка", **values)
        InlineAction(text="Кнопка", callback_data="я" * 32)
        valid = TelegramEntity(type="bold", offset=0, length=2)
        TextPart(kind="text", text="😀текст", entities=[valid])
        with self.assertRaises(ValidationError):
            TextPart(kind="text", text="😀", entities=[TelegramEntity(type="bold", offset=1, length=1)])
        with self.assertRaises(ValidationError):
            TextPart(kind="text", text="😀" * 2049)
        with self.assertRaises(ValidationError):
            TelegramEntity(type="text_link", offset=0, length=1)

    def test_images_cannot_be_lost_or_fabricated(self):
        for images in ([], [IMAGE, IMAGE], [ImageRef(image_id=uuid4(), description="Другая")]):
            with self.subTest(images=images), self.assertRaises(ValidationError):
                RagResult(answer_text=MARKDOWN, sources=[source()], images=images)
        with self.assertRaises(ValidationError):
            ConvertedDocument(markdown="![Сломано](image://не-uuid)", images=[])
        with self.assertRaises(ValidationError):
            GeneratedAnswer(kind="answered", answer_text=MARKDOWN,
                            cited_chunk_ids=[source().chunk_id], cited_image_ids=[])
        with self.assertRaises(ValidationError):
            AnsweredOutcome(kind="answered", result=RagResult(answer_text="Ответ", sources=[]))
        for score in (None, float("nan")):
            with self.subTest(score=score), self.assertRaises(ValidationError):
                InjectionResult(profile_id="guard-v1", label=None, score=score)

    def test_delivery_requires_its_own_data(self):
        base = dict(attempt=DeliveryAttempt(delivery_id=2, attempt_no=1, deadline_at=NOW),
                    user_id=7, chat_id=7)
        for fields in (
            {"purpose": "answer"}, {"purpose": "status"}, {"purpose": "ticket_link"},
            {"purpose": "busy", "next_part": 1},
            {"purpose": "busy", "parts": [TextPart(kind="text", text="Занято")], "next_part": 2},
        ):
            with self.subTest(fields=fields), self.assertRaises(ValidationError):
                ClaimedDelivery(**base, **fields)


if __name__ == "__main__":
    unittest.main()
