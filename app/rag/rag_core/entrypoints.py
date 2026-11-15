"""Contracts: docs/documents/rag-core/entrypoints.md."""
from __future__ import annotations
import asyncio
from contextlib import ExitStack
from time import monotonic
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
from rag.runtime.events import emit_event, _event_context, _context, _span, _trace_context, _utc_now
from . import exact_cache, answer_generator
from ._settings import _Settings

def _step_outcome(name, result):
    if name == "claim_question":
        return "claimed" if result is not None else "not_claimed"
    if name == "load_revision_ids":
        return "loaded" if result else "empty_corpus"
    if name == "get_answer":
        return "cache_hit" if result is not None else "cache_miss"
    if name == "search":
        return "fragments_found" if result else "no_fragments"
    if name in ("complete_question", "save_prepared_question", "schedule_question_retry"):
        return ({"complete_question": "committed", "save_prepared_question": "saved",
                 "schedule_question_retry": "retry_scheduled"}[name] if result else "stale_attempt")
    return getattr(result, "kind", "completed")


async def _step(name, operation):
    with _span(name) as record:
        result = await operation
        if record["outcome"] == "completed":
            record["outcome"] = _step_outcome(name, result)
        return result

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
    run_id = str(uuid4())
    started = monotonic()
    decision = "ack"
    status = None
    result_code = support_reason = processing_elapsed_ms = question_received_at = None
    handler_outcome, error_code = "completed", None
    started_at = _utc_now()
    steps = []
    with ExitStack() as contexts:
        contexts.enter_context(_trace_context(started, steps))
        contexts.enter_context(_event_context(**{**_context.get(), "request_id": event.request_id}))
        emit_event("rag_core", "handle_rag_question", "started", run_id, started_at=started_at)
        try:
            settings = _Settings()
            question = await _step("claim_question", storage.claim_question(event.request_id))
            if question is None:
                handler_outcome = "not_claimed"
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
                error_code = getattr(exc, "code", "question_timeout")
                emit_event("rag_core", "question_step", "finished", run_id,
                    error_code=getattr(exc, "code", "question_timeout"), retryable=retryable)
                if retryable and attempt.attempt_no < settings.max_attempts:
                    scheduled = await _step("schedule_question_retry", storage.schedule_question_retry(attempt,
                        datetime.now(timezone.utc) + timedelta(seconds=settings.retry_delay_s)))
                    handler_outcome = "retry_scheduled" if scheduled else "stale_attempt"
                    return "ack"
                outcome, query = _notice("technical_error"), None
            if outcome is None:
                handler_outcome = "stale_attempt"
                return "ack"
            committed = await _step("complete_question", storage.complete_question(attempt, outcome))
            handler_outcome = "completed" if committed else "stale_attempt"
            if committed:
                status = outcome.kind
                result_code = getattr(outcome, "code", None)
                support_reason = getattr(outcome, "reason", None)
                question_received_at = question.received_at.isoformat()
                # The persisted reception survives queue waits and retries. This
                # is time to Core commit, independent of this attempt's duration.
                elapsed = (datetime.now(timezone.utc) - question.received_at).total_seconds() * 1000
                if elapsed >= 0:
                    processing_elapsed_ms = round(elapsed, 3)
            if committed and query is not None:
                await _step("store_answer", exact_cache.store_answer(query, question.corpus_generation, outcome.result))
            return "ack"
        except StorageError:
            handler_outcome, error_code = "failed", "StorageError"
            decision = "retry_broker"
            return decision
        except asyncio.CancelledError:
            decision = None
            handler_outcome, error_code = "cancelled", "cancelled"
            raise
        except Exception as exc:
            decision = None  # The broker adapter decides after the exception escapes.
            handler_outcome, error_code = "failed", getattr(exc, "code", type(exc).__name__)
            raise
        finally:
            emit_event("rag_core", "handle_rag_question", "finished", run_id,
                consume_decision=decision, result_status=status,
                result_code=result_code, support_reason=support_reason,
                processing_elapsed_ms=processing_elapsed_ms, question_received_at=question_received_at,
                outcome=handler_outcome, error_code=error_code, started_at=started_at,
                duration_ms=round((monotonic() - started) * 1000, 3), steps=steps)
