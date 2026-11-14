"""Contracts: docs/documents/rag-core/entrypoints.md."""
from __future__ import annotations
import asyncio
from contextlib import ExitStack
from time import monotonic
from contextvars import ContextVar
from datetime import datetime, timezone, timedelta
from uuid import uuid4
from rag.contracts import (
    ConsumeDecision, RequestRef, AnsweredOutcome, NoticeOutcome, SupportOutcome,
    RagResult, SourceRef, StepError, StorageError,
)
from rag.guardrails import entrypoints as guardrails
from rag.searcher import entrypoints as searcher
from rag.model_gateway import entrypoints as models
from rag.runtime.postgresql import rag_core as storage
from rag.runtime import media
from rag.runtime.events import emit_event, _event_context, _context
from . import exact_cache, answer_generator
from ._settings import _Settings

_trace = ContextVar("rag_core_trace", default=None)

async def _step(name, operation):
    trace = _trace.get()
    started = monotonic()
    record = {"name": name, "outcome": "completed"}
    if trace is not None:
        origin, steps = trace
        record["offset_ms"] = round((started - origin) * 1000, 3)
        steps.append(record)
    try:
        result = await operation
        record["outcome"] = getattr(result, "kind", "completed")
        return result
    except BaseException as exc:
        record["outcome"] = "failed"
        record["error_code"] = getattr(exc, "code", type(exc).__name__)
        raise
    finally:
        record["duration_ms"] = round((monotonic() - started) * 1000, 3)

_NOTICES = {
    "blocked": "Не удалось принять этот вопрос. Переформулируйте его.",
    "invalid_input": "Уточните вопрос по документации.",
    "no_text": "На изображении не найден текст. Добавьте вопрос в подписи.",
    "technical_error": "Не удалось обработать вопрос из-за технической ошибки. Попробуйте позже.",
}

def _notice(code):
    return NoticeOutcome(kind="notice", code=code, text=_NOTICES[code])

def _result(generated, fragments):
    by_id = {f.chunk_id: f for f in fragments}
    try:
        cited = [by_id[i] for i in dict.fromkeys(generated.cited_chunk_ids)]
        available = {i.image_id: i for f in cited for i in f.images}
        return RagResult(answer_text=generated.answer_text,
            sources=[SourceRef.model_validate(f.model_dump(exclude={"text", "images"})) for f in cited],
            images=[available[i] for i in dict.fromkeys(generated.cited_image_ids)])
    except (KeyError, ValueError) as exc:
        raise StepError("generation_invalid_response", False) from exc

async def _pipeline(question, settings):
    raw = question.prepared_text
    if raw is None:
        if question.input.kind == "text":
            raw = question.input.text
        else:
            image = await _step("get_question_image", media.get_question_image(question.input.file_id))
            remaining = (question.attempt.deadline_at - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                raise StepError("question_timeout", True)
            ocr = await _step("recognize_text", models.recognize_text(image, timeout_s=min(settings.ocr_timeout_s, remaining)))
            raw = "\n\n".join(s.strip() for s in (question.input.caption or "", ocr.text) if s.strip())
            if not raw:
                return _notice("no_text"), None
            if not await _step("save_prepared_question", storage.save_prepared_question(question.attempt, raw)):
                return None, None
    revisions = await _step("load_revision_ids", storage.load_revision_ids(question.corpus_generation))
    checked = await _step("check_question", guardrails.check_question(raw, revisions))
    if checked.kind == "detector_unavailable":
        raise StepError("detector_unavailable", checked.retryable)
    if checked.kind in ("blocked", "invalid_input"):
        return _notice(checked.kind), None
    if not revisions:
        return SupportOutcome(kind="support", reason="no_evidence"), None
    if checked.kind == "out_of_scope":
        return SupportOutcome(kind="support", reason="out_of_scope"), None
    query = checked.normalized_question
    cached = await _step("get_answer", exact_cache.get_answer(query, question.corpus_generation))
    if cached is not None:
        return AnsweredOutcome(kind="answered", result=cached), None
    fragments = await _step("search", searcher.search(query, revisions))
    if not fragments:
        return SupportOutcome(kind="support", reason="no_evidence"), None
    generated = await _step("generate_answer", answer_generator.generate_answer(query, fragments))
    if generated.kind == "insufficient_evidence":
        return SupportOutcome(kind="support", reason="no_evidence"), None
    return AnsweredOutcome(kind="answered", result=_result(generated, fragments)), query

async def handle_rag_question(event: RequestRef) -> ConsumeDecision:
    """Process a pinned question, persist one outcome and ACK only durable transitions."""
    event = RequestRef.model_validate(event)
    settings = _Settings()
    run_id = str(uuid4())
    started = monotonic()
    decision = "ack"
    status = None
    steps = []
    trace_token = _trace.set((started, steps))
    with ExitStack() as contexts:
        contexts.enter_context(_event_context(**{**_context.get(), "request_id": event.request_id}))
        emit_event("rag_core", "handle_rag_question", "started", run_id)
        try:
            question = await _step("claim_question", storage.claim_question(event.request_id))
            if question is None:
                return "ack"
            attempt = question.attempt
            contexts.enter_context(_event_context(**{**_context.get(), "attempt_no": attempt.attempt_no}))
            remaining = (attempt.deadline_at - datetime.now(timezone.utc)).total_seconds()
            try:
                if attempt.attempt_no > settings.max_attempts:
                    raise StepError("question_attempts_exhausted", False)
                if remaining <= 0:
                    raise StepError("question_timeout", True)
                # Leave a small budget for persisting a timeout outcome/retry.
                async with asyncio.timeout(max(0.001, remaining - min(1, remaining / 10))):
                    outcome, query = await _pipeline(question, settings)
            except (StepError, TimeoutError) as exc:
                retryable = isinstance(exc, TimeoutError) or exc.retryable
                emit_event("rag_core", "question_step", "finished", run_id,
                    error_code=getattr(exc, "code", "question_timeout"), retryable=retryable)
                if retryable and attempt.attempt_no < settings.max_attempts:
                    await _step("schedule_question_retry", storage.schedule_question_retry(attempt,
                        datetime.now(timezone.utc) + timedelta(seconds=settings.retry_delay_s)))
                    return "ack"
                outcome, query = _notice("technical_error"), None
            if outcome is None:
                return "ack"
            committed = await _step("complete_question", storage.complete_question(attempt, outcome))
            if committed:
                status = outcome.kind
            if committed and query is not None:
                await _step("store_answer", exact_cache.store_answer(query, question.corpus_generation, outcome.result))
            return "ack"
        except StorageError:
            decision = "retry_broker"
            return decision
        finally:
            emit_event("rag_core", "handle_rag_question", "finished", run_id,
                consume_decision=decision, result_status=status,
                duration_ms=round((monotonic() - started) * 1000, 3), steps=steps)
            _trace.reset(trace_token)
