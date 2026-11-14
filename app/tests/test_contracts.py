"""Трассировка новых контрактов вопроса, OCR, результата и самостоятельной доставки."""

import importlib
import inspect
import unittest
from collections.abc import AsyncIterable
from pathlib import Path
from typing import get_origin, get_type_hints

from pydantic import TypeAdapter, ValidationError

from contract_examples import (
    DOCUMENT, IMAGE, MARKDOWN, NOW, REVISION, processing_attempt,
    returned, source, validate_call_arguments,
)
from rag.contracts import AllowedQuestion, AnsweredOutcome, ClaimedQuestion, DeliveryAttempt, DeliveryRef, GeneratedAnswer, ImageContent, ImageQuestion, NoticeOutcome, OutOfScope, QuestionInput, QuestionOutcome, RagResult, RequestRef, SearchFragment, StepError, StorageError, SupportOutcome, TextQuestion
from rag.dto import DTO
from rag.guardrails.entrypoints import check_question
from rag.ingestion.contracts import UploadInput
from rag.model_gateway.contracts import EmbeddingResult, OcrResult
from rag.model_gateway.entrypoints import embed_texts, recognize_text
from rag.rag_api.contracts import (
    AccessDenied, ClaimedDelivery, Conflict, ImagePart, IntakeResult, NewQuestion,
    NotFound, Sent, TextPart,
)
from rag.rag_api.entrypoints import build_delivery_parts, handle_rag_result, send_answer
from rag.rag_core.answer_generator import generate_answer
from rag.rag_core.exact_cache import get_answer, store_answer
from rag.runtime.media import get_question_image
from rag.runtime.s3 import get_image
from rag.runtime.postgresql import rag_api as pg_api, rag_core as pg_core
from rag.runtime.qdrant import QdrantHit, find_chunks, find_topic_matches
from rag.runtime.rabbitmq import (
    BrokerPublishError, OutboxNotice, QuestionNotice, ResultNotice,
)
from rag.runtime.redis import CacheUnavailable, CachedAnswer
from rag.searcher.entrypoints import search


class ContractTests(unittest.TestCase):
    def test_question_to_parts_trace(self):
        """Каждый этап получает параметры из результата уже пройденного шага."""
        incoming = NewQuestion(update_id=10, user_id=7, chat_id=7, received_at=NOW,
                               input=TextQuestion(kind="text", text="Где фильтр?"))
        validate_call_arguments(pg_api.register_question, incoming)
        intake = returned(pg_api.register_question, IntakeResult(kind="accepted", request_id=1))
        notice = QuestionNotice(message_id=REVISION, route="rag.questions",
                                event=RequestRef(request_id=intake.request_id))
        notice = TypeAdapter(OutboxNotice).validate_json(notice.model_dump_json())
        validate_call_arguments(pg_core.claim_question, notice.event.request_id)
        job = returned(pg_core.claim_question, ClaimedQuestion(
            attempt=processing_attempt(), input=incoming.input, received_at=incoming.received_at,
            corpus_generation=42))
        validate_call_arguments(pg_core.load_revision_ids, job.corpus_generation)
        revisions = returned(pg_core.load_revision_ids, frozenset({REVISION}))
        validate_call_arguments(check_question, job.input.text, revisions)
        allowed = returned(check_question, AllowedQuestion(
            kind="allowed", normalized_question=job.input.text))
        query = allowed.normalized_question
        validate_call_arguments(embed_texts, [query], "query", timeout_s=2.0)
        embedding = returned(embed_texts, EmbeddingResult(profile_id="embedding-v1", vectors=[[0.1, 0.2]]))
        validate_call_arguments(find_topic_matches, embedding.vectors[0], revisions)
        validate_call_arguments(get_answer, query, job.corpus_generation)
        self.assertIsNone(returned(get_answer, None))
        validate_call_arguments(search, query, revisions)
        validate_call_arguments(find_chunks, query, embedding.vectors[0], revisions, 10,
                                lexical_priority=False)
        fragment = SearchFragment(**source().model_dump(), text=MARKDOWN, images=[IMAGE])
        hits = returned(find_chunks, [QdrantHit(fragment=fragment, score=0.72)])
        fragments = returned(search, [hit.fragment for hit in hits])
        validate_call_arguments(generate_answer, query, fragments)
        answer = returned(generate_answer, GeneratedAnswer(
            kind="answered", answer_text=MARKDOWN,
            cited_chunk_ids=[fragments[0].chunk_id], cited_image_ids=[IMAGE.image_id]))
        result = RagResult(answer_text=answer.answer_text, sources=[source()], images=[IMAGE])
        outcome = AnsweredOutcome(kind="answered", result=result)
        validate_call_arguments(pg_core.complete_question, job.attempt, outcome)
        validate_call_arguments(store_answer, query, job.corpus_generation, result)
        cached = CachedAnswer(corpus_generation=42, **result.model_dump())
        self.assertEqual(returned(get_answer, RagResult(
            **cached.model_dump(include=set(RagResult.model_fields)))), result)

        # Очередь результата ссылается на доставку, а не на request_id.
        delivery_event = ResultNotice(message_id=DOCUMENT, route="rag.results",
                                      event=DeliveryRef(delivery_id=2))
        delivery_event = TypeAdapter(OutboxNotice).validate_json(delivery_event.model_dump_json())
        validate_call_arguments(handle_rag_result, delivery_event.event)
        validate_call_arguments(pg_api.claim_delivery, delivery_event.event.delivery_id)
        attempt = DeliveryAttempt(delivery_id=2, attempt_no=1, deadline_at=NOW)
        restored = RagResult.model_validate_json(result.model_dump_json())
        delivery = returned(pg_api.claim_delivery, ClaimedDelivery(
            attempt=attempt, user_id=7, chat_id=7, request_id=1, purpose="answer", content=restored))
        validate_call_arguments(build_delivery_parts, delivery, "https://bot.example")
        parts = returned(build_delivery_parts, [
            TextPart(kind="text", text="Откройте фильтр. Схема согласования"),
            ImagePart(kind="image", image=IMAGE),
        ])
        validate_call_arguments(pg_api.store_delivery_parts, attempt, parts)
        for number, part in enumerate(parts):
            validate_call_arguments(send_answer, delivery.chat_id, part)
            receipt = returned(send_answer, Sent(kind="sent", message_id=77 + number))
            validate_call_arguments(pg_api.record_delivery_part, attempt, number,
                                    receipt.message_id, receipt.variant)
        validate_call_arguments(get_image, parts[1].image.image_id)
        validate_call_arguments(pg_api.complete_delivery, attempt, True, False)

    def test_ocr_is_available_before_guardrails(self):
        image_input = ImageQuestion(kind="image", file_id="telegram-file", caption="Ошибка входа")
        restored = TypeAdapter(QuestionInput).validate_json(image_input.model_dump_json())
        validate_call_arguments(get_question_image, restored.file_id)
        image = returned(get_question_image, ImageContent(data=b"\xff\xd8", mime_type="image/jpeg"))
        validate_call_arguments(recognize_text, image, timeout_s=3.0)
        observation = returned(recognize_text, OcrResult(profile_id="ocr-v1", text="AUTH-17"))
        prepared_text = restored.caption + "\n" + observation.text
        validate_call_arguments(pg_core.save_prepared_question, processing_attempt(), prepared_text)
        resumed = ClaimedQuestion(attempt=processing_attempt(), input=restored, received_at=NOW,
                                  corpus_generation=42, prepared_text=prepared_text)
        validate_call_arguments(check_question, resumed.prepared_text, frozenset({REVISION}))
        self.assertEqual(returned(recognize_text, OcrResult(profile_id="ocr-v1", text="")).text, "")

    def test_short_paths_and_old_contracts(self):
        for outcome in (
            SupportOutcome(kind="support", reason="out_of_scope"),
            SupportOutcome(kind="support", reason="no_evidence"),
            NoticeOutcome(kind="notice", code="technical_error", text="Повторите позже"),
        ):
            restored = TypeAdapter(QuestionOutcome).validate_json(outcome.model_dump_json())
            validate_call_arguments(pg_core.complete_question, processing_attempt(), restored)
        self.assertIsInstance(returned(check_question, OutOfScope(kind="out_of_scope")), OutOfScope)
        validate_call_arguments(search, "вопрос", frozenset())
        self.assertEqual(returned(search, []), [])
        with self.assertRaises(ValidationError):
            validate_call_arguments(handle_rag_result, RequestRef(request_id=1))
        with self.assertRaises(ValidationError):
            validate_call_arguments(pg_api.complete_delivery, processing_attempt(), True, False)
        with self.assertRaises(TypeError):
            validate_call_arguments(check_question, "нет набора редакций")

    def test_public_exceptions_and_retry_contract(self):
        for error in (StepError, StorageError, BrokerPublishError, CacheUnavailable,
                      AccessDenied, NotFound, Conflict):
            self.assertTrue(issubclass(error, Exception))
        failure = StepError("chunk_count_mismatch", retryable=False)
        self.assertEqual((failure.code, failure.retryable, str(failure)),
                         ("chunk_count_mismatch", False, "chunk_count_mismatch"))

    def test_all_models_and_annotations_resolve(self):
        app = Path(__file__).parents[1]
        for file in (app / "rag").rglob("*.py"):
            module = importlib.import_module(".".join(file.relative_to(app).with_suffix("").parts))
            for value in vars(module).values():
                if getattr(value, "__module__", None) != module.__name__:
                    continue
                if inspect.isclass(value) and issubclass(value, DTO):
                    value.model_json_schema()
                if inspect.isfunction(value):
                    for annotation in get_type_hints(value, include_extras=True).values():
                        if annotation is UploadInput or get_origin(annotation) is AsyncIterable:
                            continue
                        TypeAdapter(annotation)


if __name__ == "__main__":
    unittest.main()
