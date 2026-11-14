"""Contracts: docs/documents/infrastructure/postgresql/rag-core.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from datetime import datetime
from uuid import UUID

from rag.contracts import ClaimedQuestion, ProcessingAttempt, QuestionOutcome, QuestionInput

from datetime import timedelta
from uuid import uuid4
from rag.contracts import RagResult
from rag.runtime._state import _get
from rag.runtime._validation import _positive, _utc, _text
from ._db import (_transaction, _question_row, _all, _execute, _now, _corpus,
                  _json, _decode, _delivery, _outbox, _valid_attempt)


async def claim_question(request_id: int) -> ClaimedQuestion | None:
    """Атомарно взять первый доступный вопрос чата и закрепить поколение корпуса.

    Args:
        request_id: ID сохранённого вопроса.

    Returns:
        ClaimedQuestion или None.
    """
    _positive(request_id)
    async with _transaction() as connection:
        row = await _question_row(connection, request_id)
        if row is None or row["state"] not in ("queued", "processing"):
            return None
        corpus = await _corpus(connection)
        now = await _now(connection)
        if row["resume_at"] is not None and row["resume_at"] > now:
            return None
        generation = row["pinned_generation"]
        if generation is None:
            generation = corpus["generation"]
        deadline = now + timedelta(seconds=_get().settings.question_timeout_s)
        attempt_no = row["attempt_no"] + 1
        await _execute(connection, """UPDATE rag_requests SET state='processing',attempt_no=:number,
            resume_at=:deadline,pinned_generation=:generation WHERE request_id=:id""",
            number=attempt_no, deadline=deadline, generation=generation, id=request_id)
        return ClaimedQuestion(attempt=ProcessingAttempt(request_id=request_id, attempt_no=attempt_no,
            deadline_at=deadline), input=_decode(QuestionInput, row["input"]),
            received_at=row["received_at"], corpus_generation=generation, prepared_text=row["prepared_text"])


async def save_prepared_question(attempt: ProcessingAttempt, text: str) -> bool:
    """Сохранить подпись и OCR по действующей попытке для повторов после рестарта.

    Args:
        attempt: Действующая попытка и объединённый OCR-текст/подпись.
        text: Действующая попытка и объединённый OCR-текст/подпись.

    Returns:
        bool.
    """
    _text(text)
    async with _transaction() as connection:
        row = await _question_row(connection, attempt.request_id)
        if not _valid_attempt(row, attempt, "processing", await _now(connection)):
            return False
        if row["prepared_text"] is not None and row["prepared_text"] != text:
            raise ValueError("Prepared question text is immutable")
        await _execute(connection, "UPDATE rag_requests SET prepared_text=:text WHERE request_id=:id",
                       text=text, id=attempt.request_id)
        return True


async def load_revision_ids(corpus_generation: int) -> frozenset[UUID]:
    """Получить редакции, видимые в закреплённом поколении корпуса.

    Args:
        corpus_generation: Закреплённый состав.

    Returns:
        frozenset UUID.
    """
    if type(corpus_generation) is not int or corpus_generation < 0:
        raise ValueError("Invalid corpus generation")
    async with _transaction() as connection:
        rows = await _all(connection, """SELECT revision_id FROM document_revisions
            WHERE visible_from<=:generation AND (visible_until IS NULL OR :generation<visible_until)""",
            generation=corpus_generation)
        return frozenset(row["revision_id"] for row in rows)


async def complete_question(attempt: ProcessingAttempt, outcome: QuestionOutcome) -> bool:
    """Сохранить QuestionOutcome и создать доставку либо намерение Support.

    Args:
        attempt: Право обработки.
        outcome: AnsweredOutcome / NoticeOutcome / SupportOutcome.

    Returns:
        bool.
    """
    outcome = _decode(QuestionOutcome, outcome.model_dump_json())
    async with _transaction() as connection:
        row = await _question_row(connection, attempt.request_id)
        if row is None:
            return False
        await _corpus(connection)
        if not _valid_attempt(row, attempt, "processing", await _now(connection)):
            return False
        if outcome.kind == "support":
            await _execute(connection, """INSERT INTO support_cases(case_id,request_id,user_id,state,reason)
                VALUES(:case,:request,:user,'creating',:reason)
                ON CONFLICT(request_id) DO UPDATE SET state='creating',reason=EXCLUDED.reason,
                check_at=NULL WHERE support_cases.state NOT IN ('open','closed')""",
                case=uuid4(), request=attempt.request_id, user=row["user_id"], reason=outcome.reason)
            state = "awaiting_support"
        else:
            content = outcome.result if outcome.kind == "answered" else RagResult(answer_text=outcome.text, sources=[])
            await _delivery(connection, user_id=row["user_id"], chat_id=row["chat_id"],
                request_id=attempt.request_id, purpose="answer" if outcome.kind == "answered" else "notice",
                content=content, terminal=True, key="terminal:" + str(attempt.request_id))
            state = "awaiting_delivery"
        await _execute(connection, """UPDATE rag_requests SET state=:state,result=CAST(:result AS jsonb),
            pinned_generation=NULL,resume_at=NULL WHERE request_id=:id""",
            state=state, result=_json(outcome), id=attempt.request_id)
        return True


async def schedule_question_retry(attempt: ProcessingAttempt, retry_at: datetime) -> bool:
    """Сохранить отложенный повтор обработки с outbox rag.questions.

    Args:
        attempt: Текущая попытка и следующий запуск.
        retry_at: Текущая попытка и следующий запуск.

    Returns:
        bool.
    """
    _utc(retry_at)
    async with _transaction() as connection:
        row = await _question_row(connection, attempt.request_id)
        if not _valid_attempt(row, attempt, "processing", await _now(connection)):
            return False
        await _execute(connection, "UPDATE rag_requests SET state='queued',resume_at=:at WHERE request_id=:id",
            at=retry_at, id=attempt.request_id)
        await _outbox(connection, "rag.questions", attempt.request_id, retry_at)
        return True
