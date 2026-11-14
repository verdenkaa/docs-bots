"""Guardrails: временная реализация допускает любой непустой вопрос без изменений."""

import unittest
from uuid import uuid4

from rag.contracts import AllowedQuestion, RejectedQuestion
from rag.guardrails.entrypoints import check_question


class GuardrailsStubTests(unittest.IsolatedAsyncioTestCase):
    async def test_question_is_allowed_unchanged(self) -> None:
        for question in ("Почему не работает фильтр в БО \"!> 200\"?", "Ошибка AUTH-17", "--force и --no-force"):
            with self.subTest(question=question):
                result = await check_question(question, frozenset({uuid4()}))
                self.assertEqual(result, AllowedQuestion(kind="allowed", normalized_question=question))

    async def test_empty_corpus_is_allowed_too(self) -> None:
        result = await check_question("Где фильтр?", frozenset())
        self.assertEqual(result.kind, "allowed")

    async def test_blank_question_is_invalid_input(self) -> None:
        for question in ("", "   ", "\n\t"):
            with self.subTest(question=repr(question)):
                result = await check_question(question, frozenset())
                self.assertEqual(result, RejectedQuestion(kind="invalid_input"))


if __name__ == "__main__":
    unittest.main()
