"""Exact shortcut classification and atomic local delivery, without services."""

from contextlib import asynccontextmanager, redirect_stdout
from datetime import datetime, timezone
import io
import re
import unittest
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError

from rag.contracts import StorageError, TextQuestion
from rag.rag_api import entrypoints, intake, support
from rag.rag_api.contracts import IntakeResult, NewQuestion
from rag.rag_api.shortcuts import ShortcutResult, intercept_message
from rag.rag_api.shortcuts import interceptor, registry
from rag.runtime.postgresql import rag_api as repository


def question():
    return NewQuestion(update_id=17, user_id=7, chat_id=7, username="employee",
                       received_at=datetime.now(timezone.utc),
                       input=TextQuestion(kind="text", text="Привет!"))


class ShortcutTests(unittest.TestCase):
    def test_normalization_and_strict_misses(self):
        for text, expected in (("ПРИВЕТ!!!", "greeting"), ("  Что\tты умеешь??", "bot_capabilities"),
                               ("Позови, оператора!", "call_operator"),
                               ("Где источники?", "answer_no_sources"),
                               ("Объясни подробнее", "request_more_detail"),
                               ("Неверный ответ", "answer_incorrect")):
            with self.subTest(text=text):
                self.assertEqual(intercept_message(text).intent, expected)
        self.assertEqual(interceptor.normalize("Ёщё__раз!"), "еще раз")
        self.assertEqual(interceptor.normalize("ＨＥＬＬＯ！"), "hello")
        for text in (None, "", " \n\t", "!!!", "Не вызывай оператора", "Как настроить интеграцию?",
                     "Привет, как настроить систему?", "спасибочки за интеграцию", "превет"):
            with self.subTest(text=text):
                self.assertIsNone(intercept_message(text))

    def test_every_supplied_phrase_resolves_to_its_intent_and_action(self):
        for intent, phrases in registry.PHRASES.items():
            for phrase in re.split(r"[\n|]", phrases):
                if not phrase.strip():
                    continue
                with self.subTest(intent=intent, phrase=phrase):
                    result = intercept_message(phrase)
                    self.assertEqual(result.intent, intent)
                    self.assertEqual(result.action, "operator" if intent in registry.OPERATOR_INTENTS else "reply")
                    if result.action == "reply":
                        expected = registry.ISSUE_REPLY if intent in registry.ISSUE_INTENTS else registry.REPLIES[intent]
                        self.assertEqual(result.response, expected)
                    else:
                        self.assertIsNone(result.response)

    def test_registry_rejects_collisions_unknown_and_empty_phrases(self):
        for phrases in ({"greeting": "Привет!", "thanks": "ПРИВЕТ"},
                        {"unregistered": "test"}, {"greeting": "!!!"}):
            with self.subTest(phrases=phrases), patch.object(registry, "PHRASES", phrases):
                with self.assertRaises(ValueError):
                    interceptor._build_shortcuts()
        with patch.object(registry, "OPERATOR_INTENTS", {"greeting"}):
            with self.assertRaises(ValueError):
                interceptor._build_shortcuts()

    def test_result_validation_and_registry_isolation(self):
        for values in (dict(intent="test", action="reply"),
                       dict(intent="test", action="operator", response="Text")):
            with self.assertRaises(ValidationError):
                ShortcutResult(**values)
        result = intercept_message("привет")
        result.response = "Changed by a caller"
        self.assertEqual(intercept_message("привет").response, registry.REPLIES["greeting"])


class ShortcutPersistenceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        @asynccontextmanager
        async def transaction():
            yield object()

        self.enterContext(patch.object(repository, "_transaction", transaction))
        self.prior = self.enterContext(patch.object(repository, "_prior_update", AsyncMock(return_value=None)))
        self.lock = self.enterContext(patch.object(repository, "_lock", AsyncMock()))
        self.access = self.enterContext(patch.object(repository, "_one", AsyncMock(return_value={"enabled": True})))
        self.delivery = self.enterContext(patch.object(repository, "_delivery", AsyncMock(return_value=41)))
        self.save = self.enterContext(patch.object(repository, "_save_update", AsyncMock()))
        self.outbox = self.enterContext(patch.object(repository, "_outbox", AsyncMock()))

    async def test_reply_records_standalone_notice_and_ledger(self):
        result = await repository.register_shortcut_reply(question(), "Готовый ответ")
        self.assertEqual(result, IntakeResult(kind="handled", delivery_id=41))
        values = self.delivery.await_args.kwargs
        self.assertEqual((values["purpose"], values["content"].answer_text), ("notice", "Готовый ответ"))
        self.assertEqual(values["content"].sources, [])
        self.assertNotIn("request_id", values)
        self.save.assert_awaited_once()
        self.outbox.assert_not_awaited()  # _delivery owns the existing rag.results outbox.

    async def test_deduplication_precedes_changed_access(self):
        previous = IntakeResult(kind="handled", delivery_id=23)
        self.prior.return_value = previous
        for operation in (repository.check_message,
                          lambda value: repository.register_shortcut_reply(value, "Ответ")):
            self.assertEqual(await operation(question()), previous)
        self.access.assert_not_awaited()
        self.delivery.assert_not_awaited()
        self.save.assert_not_awaited()

    async def test_access_is_checked_again_when_committing_reply(self):
        self.assertIsNone(await repository.check_message(question()))
        self.save.assert_not_awaited()
        self.access.return_value = {"enabled": False}
        result = await repository.register_shortcut_reply(question(), "Ответ")
        self.assertEqual(result.kind, "denied")
        self.assertEqual(self.delivery.await_args.kwargs["purpose"], "denied")
        self.save.assert_awaited_once()

    async def test_no_username_is_denied(self):
        incoming = question().model_copy(update={"username": None})
        self.access.return_value = None
        self.assertEqual((await repository.check_message(incoming)).kind, "denied")
        self.assertEqual(self.delivery.await_args.kwargs["purpose"], "denied")


    async def test_bot_navigation_needs_no_admission_and_preserves_old_ledger(self):
        incoming = question().model_copy(update={"username": None})
        result = await repository.register_bot_notice(incoming, "Откройте мини-приложение")
        self.assertEqual(result, IntakeResult(kind="handled", delivery_id=41))
        self.access.assert_not_awaited()
        self.assertNotIn("request_id", self.delivery.await_args.kwargs)
        self.prior.return_value = IntakeResult(kind="duplicate", request_id=9)
        self.assertEqual((await repository.register_bot_notice(incoming, "Другой текст")).request_id, 9)
        self.assertEqual(self.delivery.await_count, 1)


class BotNavigationIntakeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.output = io.StringIO()
        self.enterContext(redirect_stdout(self.output))
        self.reply = self.enterContext(patch.object(repository, "register_bot_notice", AsyncMock(
            return_value=IntakeResult(kind="handled", delivery_id=41))))
        self.rag = self.enterContext(patch.object(repository, "register_question", AsyncMock(
            side_effect=AssertionError("Telegram must never enqueue RAG"))))
        self.support = self.enterContext(patch.object(support, "handle_support_action", AsyncMock()))

    def update(self, text):
        return {"update_id": 17, "message": {"from": {"id": 7, "username": "Employee"},
                "chat": {"id": 7, "type": "private"}, "text": text}}

    async def test_every_direct_text_only_invites_to_mini_app(self):
        for text in ("Привет!", "Как настроить систему?", "Позови оператора", "Где источники?",
                     "Что означает /start в инструкции?", "/start", "/help", "/start@DocsBot token"):
            with self.subTest(text=text):
                result = await entrypoints.handle_telegram_update(self.update(text))
                self.assertEqual(result, IntakeResult(kind="handled", delivery_id=41))
                self.assertIn("мини-приложени", self.reply.await_args.args[1])
        self.rag.assert_not_awaited()
        self.support.assert_not_awaited()
        self.assertNotIn("Как настроить систему", self.output.getvalue())

    async def test_images_also_only_invite_to_mini_app(self):
        update = self.update("Привет")
        update["message"].update(photo=[{"file_id": "photo", "width": 10, "height": 10}], caption="Позови оператора")
        self.assertEqual((await entrypoints.handle_telegram_update(update)).kind, "handled")
        self.assertEqual(self.reply.await_args.args[0].input.file_id, "photo")
        self.rag.assert_not_awaited()
        self.support.assert_not_awaited()

    async def test_unknown_commands_are_silent(self):
        with patch.object(repository, "record_update", AsyncMock(return_value=IntakeResult(kind="ignored"))) as record:
            for text in ("/unknown", "/support", "/operator", "/helpful", "/start_invalid",
                         "/unknown@DocsBot payload", "/start@", "/start/foo"):
                with self.subTest(text=text):
                    self.assertEqual((await entrypoints.handle_telegram_update(self.update(text))).kind, "ignored")
                    self.assertEqual(record.await_args.args[-1], "ignored")
        self.reply.assert_not_awaited()
        self.rag.assert_not_awaited()
        self.support.assert_not_awaited()

    async def test_navigation_does_not_require_username(self):
        update = self.update("/start")
        update["message"]["from"] = {"id": 7}
        self.assertEqual((await entrypoints.handle_telegram_update(update)).kind, "handled")
        self.assertIsNone(self.reply.await_args.args[0].username)

    async def test_storage_error_does_not_fall_back_to_rag(self):
        self.reply.side_effect = StorageError()
        self.assertEqual((await entrypoints.handle_telegram_update(self.update("привет"))).kind, "retry_later")
        self.rag.assert_not_awaited()
