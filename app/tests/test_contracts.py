"""Проверки контрактов на примерах данных; сам RAG-пайплайн здесь не реализуется."""

import ast
import re
import importlib
import inspect
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import get_type_hints, get_origin
import unittest
from uuid import uuid4, uuid5

from pydantic import TypeAdapter, ValidationError
from collections.abc import AsyncIterable

from rag.contracts import (
    AllowedQuestion, ClaimedQuestion, GeneratedAnswer, GuardrailsResult,
    ProcessingAttempt, RagResult, RejectedQuestion, RequestRef, SourceRef,
)
from rag.guardrails.entrypoints import check_question
from rag.ingestion.contracts import IngestionAttempt, JobRef, PreparedChunk, StagedUpload, UploadSource, UploadInput
from rag.ingestion.worker import prepare_chunks
from rag.ingestion.gateway import accept_upload
from rag.runtime.upload_spool import stage_upload
from rag.model_gateway.contracts import EmbeddingResult, InjectionResult, ModelGenerationResult
from rag.model_gateway.entrypoints import embed_texts, classify_injection, generate, rerank
from rag.rag_api.contracts import ClaimedDelivery
from rag.rag_api.entrypoints import send_answer
from rag.rag_core.answer_generator import generate_answer
from rag.rag_core.exact_cache import get_answer
from rag.runtime.postgresql import ingestion as pg_ingestion, rag_core as pg_core
from rag.runtime.qdrant import ChunkPoint, QdrantHit, find_chunks, write_revision_points
from rag.runtime.rabbitmq import OutboxNotice, RagNotice, IngestionNotice
from rag.runtime.redis import CachedAnswer
from rag.searcher.entrypoints import search


def call(function, *args, **kwargs):
    """Сопоставить аргументы с реальной сигнатурой и проверить доступные на этом шаге данные."""
    bound = inspect.signature(function).bind(*args, **kwargs)
    for name, value in bound.arguments.items():
        TypeAdapter(get_type_hints(function, include_extras=True)[name]).validate_python(value)


def returned(function, value):
    return TypeAdapter(get_type_hints(function, include_extras=True)["return"]).validate_python(value)


def contract_shape(node):
    """Сравнить структуру контрактов, игнорируя только пояснения и описания Field.

    Field(default=None) сохраняет значение по умолчанию; обязательные поля остаются обязательными.
    Ограничения валидации и конфигурация модели сохраняются при сравнении.
    """
    class WithoutProse(ast.NodeTransformer):
        def visit_Expr(self, item):
            if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                return None
            return self.generic_visit(item)

        def visit_AnnAssign(self, item):
            item = self.generic_visit(item)
            value = item.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "Field":
                if not value.args and not value.keywords:
                    item.value = None
                elif not value.args and len(value.keywords) == 1 and value.keywords[0].arg == "default":
                    item.value = value.keywords[0].value
            return item

        def visit_Call(self, item):
            item = self.generic_visit(item)
            if isinstance(item.func, ast.Name) and item.func.id == "Field":
                item.keywords = [kw for kw in item.keywords if kw.arg != "description"]
            return item

    # Разбираем копию: NodeTransformer изменяет переданное ему дерево.
    return ast.dump(WithoutProse().visit(ast.parse(ast.unparse(node))).body[0])


class ContractTests(unittest.TestCase):
    def setUp(self):
        self.attempt = ProcessingAttempt(request_id=1, attempt_no=1, deadline_at=datetime.now(timezone.utc))
        self.revision = uuid4()

    def test_question_trace(self):
        envelope = RagNotice(message_id=uuid4(), route="rag.questions", event=RequestRef(request_id=1))
        envelope = TypeAdapter(OutboxNotice).validate_json(envelope.model_dump_json())
        call(pg_core.claim_question, envelope.event.request_id)
        question = returned(pg_core.claim_question, ClaimedQuestion(
            attempt=self.attempt, raw_question="Как установить?", corpus_generation=1))
        call(check_question, question.raw_question)
        call(classify_injection, question.raw_question, timeout_s=2.0)
        observation = InjectionResult(profile_id="guard-v1", label="benign", score=None)
        self.assertEqual(observation.label, "benign")
        allowed = returned(check_question, AllowedQuestion(kind="allowed", normalized_question=question.raw_question))
        query = allowed.normalized_question
        call(get_answer, query, question.corpus_generation)
        self.assertIsNone(returned(get_answer, None))
        call(pg_core.load_revision_ids, question.corpus_generation)
        revisions = returned(pg_core.load_revision_ids, frozenset({self.revision}))
        call(search, query, revisions)
        call(embed_texts, [query], "query", timeout_s=5.0)
        embedding = returned(embed_texts, EmbeddingResult(profile_id="emb-v1", vectors=[[0.1, 0.2]]))
        call(find_chunks, query, embedding.vectors[0], revisions, 10)
        point = ChunkPoint(chunk_id=uuid5(self.revision, "0"), document_id=uuid4(),
            revision_id=self.revision, source_name="guide", locator=None, source_url=None,
            text="Инструкция", chunk_no=0, dense=embedding.vectors[0])
        hits = returned(find_chunks, [QdrantHit(fragment=point, score=1.0)])
        call(rerank, query, [hit.fragment.text for hit in hits], timeout_s=5.0)
        fragments = returned(search, [hit.fragment for hit in hits])
        call(generate_answer, query, fragments)
        call(generate, [{"role": "user", "content": query}],
             {"max_output_tokens": 100, "temperature": None}, timeout_s=30.0)
        output = ModelGenerationResult(profile_id="gen-v1", text='{"kind":"answered"}',
            finish_reason="completed", usage=None)
        self.assertEqual(output.finish_reason, "completed")
        answer = returned(generate_answer, GeneratedAnswer(kind="answered", answer_text="Ответ",
            cited_chunk_ids=[fragments[0].chunk_id]))
        by_id = {fragment.chunk_id: fragment for fragment in fragments}
        result = RagResult(answer_text=answer.answer_text, sources=[
            SourceRef.model_validate(by_id[cid].model_dump(include=set(SourceRef.model_fields)))
            for cid in answer.cited_chunk_ids])
        call(pg_core.complete_question, question.attempt, result)
        # Сохраняем JSON и читаем его заново для отдельной попытки доставки.
        restored = RagResult.model_validate_json(result.model_dump_json())
        delivery = ClaimedDelivery(attempt=self.attempt, chat_id=-100, result=restored)
        call(send_answer, delivery.chat_id, delivery.result)
        self.assertEqual(restored.sources[0].chunk_id, point.chunk_id)

    def test_ingestion_trace(self):
        source = UploadSource(filename="guide.txt", source_url=None)
        upload = StagedUpload(path=Path("source"), source=source)
        call(prepare_chunks, upload)
        chunks = returned(prepare_chunks, [PreparedChunk(text="Инструкция", locator=None)])
        call(embed_texts, [c.text for c in chunks], "document", timeout_s=5.0)
        embedding = returned(embed_texts, EmbeddingResult(profile_id="emb-v1", vectors=[[0.1]]))
        attempt = IngestionAttempt(job_id=self.revision, attempt_no=1, deadline_at=self.attempt.deadline_at)
        call(pg_ingestion.begin_revision_write, attempt, len(chunks))
        points = [ChunkPoint(chunk_id=uuid5(self.revision, str(i)), document_id=uuid4(),
            revision_id=self.revision, source_name=source.filename, source_url=source.source_url,
            locator=chunk.locator, text=chunk.text, chunk_no=i, dense=embedding.vectors[i])
            for i, chunk in enumerate(chunks)]
        call(write_revision_points, self.revision, points)
        call(pg_ingestion.publish_revision, attempt)
        notice = IngestionNotice(message_id=uuid4(), route="ingestion.jobs", event=JobRef(job_id=self.revision))
        self.assertEqual(TypeAdapter(OutboxNotice).validate_json(notice.model_dump_json()).event.job_id, self.revision)

    def test_upload_stream_is_not_a_json_message(self):
        async def content():
            yield b"guide"
        upload = UploadInput(content=content(), source=UploadSource(filename="guide.txt", source_url=None), document_id=None)
        inspect.signature(accept_upload).bind(upload)
        bound = inspect.signature(stage_upload).bind(self.revision, upload.content, upload.source)
        self.assertIsInstance(bound.arguments["content"], AsyncIterable)
        self.assertIsInstance(upload.source, UploadSource)
        with self.assertRaises(TypeError):
            json.dumps(upload)

    def test_short_paths_and_empty_corpus(self):
        question = ClaimedQuestion(attempt=self.attempt, raw_question=" ", corpus_generation=0)
        call(check_question, question.raw_question)
        rejected = TypeAdapter(GuardrailsResult).validate_json('{"kind":"invalid_input"}')
        self.assertIsInstance(rejected, RejectedQuestion)
        call(get_answer, "вопрос", 0)
        call(search, "вопрос", frozenset())
        self.assertEqual(returned(search, []), [])
        cached = CachedAnswer(answer_text="Ответ", sources=[], corpus_generation=0)
        # Явно выбираем поля: служебные поля кеша не должны попадать в строгий RagResult.
        result = RagResult(answer_text=cached.answer_text, sources=cached.sources)
        call(pg_core.complete_question, self.attempt, result)

    def test_reject_malformed_boundaries(self):
        for raw in ['{"request_id":"1"}', '{"request_id":true}', '{"request_id":0}', '{"request_id":1,"extra":0}']:
            with self.subTest(raw=raw), self.assertRaises(ValidationError):
                RequestRef.model_validate_json(raw)
        with self.assertRaises(ValidationError):
            TypeAdapter(OutboxNotice).validate_json(json.dumps({"message_id":str(uuid4()),
                "route":"ingestion.jobs", "event":{"request_id":1}}))
        with self.assertRaises(ValidationError):
            InjectionResult(profile_id="guard-v1", label=None, score=None)
        with self.assertRaises(ValidationError):
            InjectionResult(profile_id="guard-v1", label=None, score=float("nan"))
        with self.assertRaises(ValidationError):
            UploadSource(filename="guide.txt")  # поле source_url обязательно, хотя допускает None
        with self.assertRaises(ValidationError):
            ModelGenerationResult(profile_id="gen-v1", text="", finish_reason="completed", usage=None)
        with self.assertRaises(ValidationError):
            GeneratedAnswer(kind="answered", answer_text="Ответ", cited_chunk_ids=[])
        with self.assertRaises(ValidationError):
            ProcessingAttempt(request_id=1, attempt_no=1, deadline_at=datetime.now())

    def test_documented_dto_definitions_match_source(self):
        app = Path(__file__).parents[1]
        definitions = {}
        for file in (app / "rag").rglob("*.py"):
            for node in ast.parse(file.read_text(encoding="utf-8")).body:
                if isinstance(node, ast.ClassDef) and node.name not in {"StepError", "StorageError", "BrokerPublishError"}:
                    definitions[node.name] = contract_shape(node)
        checked = 0
        for file in (app.parent / "docs/documents").rglob("*.md"):
            if "drafts" in file.parts:
                continue
            for code in re.findall(r"~~~python\n(.*?)\n~~~", file.read_text(encoding="utf-8"), re.S):
                try:
                    nodes = ast.parse(code).body
                except SyntaxError:
                    continue  # намеренно сокращённые примеры документации
                for node in nodes:
                    if isinstance(node, ast.ClassDef) and node.name in definitions:
                        self.assertEqual(contract_shape(node), definitions[node.name], f"{file}: {node.name}")
                        checked += 1
        self.assertGreater(checked, 30)

    def test_all_models_and_signatures_resolve(self):
        from rag.dto import DTO
        for file in (Path(__file__).parents[1] / "rag").rglob("*.py"):
            module = importlib.import_module(".".join(file.relative_to(Path(__file__).parents[1]).with_suffix("").parts))
            for value in vars(module).values():
                if inspect.isclass(value) and issubclass(value, DTO):
                    value.model_json_schema()
                if inspect.iscoroutinefunction(value):
                    for annotation in get_type_hints(value, include_extras=True).values():
                        if annotation is UploadInput or get_origin(annotation) is AsyncIterable:
                            continue  # технические потоки Python не являются JSON DTO
                        TypeAdapter(annotation)


if __name__ == "__main__":
    unittest.main()
