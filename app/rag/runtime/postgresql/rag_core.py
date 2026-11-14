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
from ._db import (
    _transaction,
    _question_row,
    _all,
    _execute,
    _now,
    _corpus,
    _json,
    _decode,
    _delivery,
    _outbox,
    _valid_attempt,
)
from sqlalchemy import select
from ._models import _SupportCase

from sqlalchemy import or_, update
from ._models import _Request, _Revision
from ._db import _data


async def claim_question(request_id: int) -> ClaimedQuestion | None:
    """Атомарно взять первый доступный вопрос чата и закрепить поколение корпуса.

    Args:
        request_id: ID сохранённого вопроса.

    Returns:
        ClaimedQuestion или None.
    """
    _positive(request_id)
    async with _transaction() as session:
        row = await _question_row(session, request_id)
        if row is None or row["state"] not in ("queued", "processing"):
            return None
        corpus = await _corpus(session)
        now = await _now(session)
        if row["resume_at"] is not None and row["resume_at"] > now:
            return None
        generation = row["pinned_generation"]
        if generation is None:
            generation = corpus["generation"]
        deadline = now + timedelta(seconds=_get().settings.question_timeout_s)
        attempt_no = row["attempt_no"] + 1
        await _execute(
            session,
            update(_Request)
            .where(_Request.request_id == request_id)
            .values(
                state="processing",
                attempt_no=attempt_no,
                resume_at=deadline,
                pinned_generation=generation,
            ),
        )
        return ClaimedQuestion(
            attempt=ProcessingAttempt(
                request_id=request_id, attempt_no=attempt_no, deadline_at=deadline
            ),
            input=_decode(QuestionInput, row["input"]),
            received_at=row["received_at"],
            corpus_generation=generation,
            prepared_text=row["prepared_text"],
        )


async def save_prepared_question(attempt: ProcessingAttempt, text: str) -> bool:
    """Сохранить подпись и OCR по действующей попытке для повторов после рестарта.

    Args:
        attempt: Действующая попытка и объединённый OCR-текст/подпись.
        text: Действующая попытка и объединённый OCR-текст/подпись.

    Returns:
        bool.
    """
    _text(text)
    async with _transaction() as session:
        row = await _question_row(session, attempt.request_id)
        if not _valid_attempt(row, attempt, "processing", await _now(session)):
            return False
        if row["prepared_text"] is not None and row["prepared_text"] != text:
            raise ValueError("Prepared question text is immutable")
        await _execute(
            session,
            update(_Request)
            .where(_Request.request_id == attempt.request_id)
            .values(prepared_text=text),
        )
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
    async with _transaction() as session:
        rows = await _all(
            session,
            select(_Revision.revision_id).where(
                _Revision.visible_from <= corpus_generation,
                or_(_Revision.visible_until.is_(None), _Revision.visible_until > corpus_generation),
            ),
        )
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
    async with _transaction() as session:
        row = await _question_row(session, attempt.request_id)
        if row is None:
            return False
        await _corpus(session)
        if not _valid_attempt(row, attempt, "processing", await _now(session)):
            return False
        if outcome.kind == "support":
            case = await session.scalar(
                select(_SupportCase).where(_SupportCase.request_id == attempt.request_id)
            )
            if case is None:
                session.add(
                    _SupportCase(
                        case_id=uuid4(),
                        request_id=attempt.request_id,
                        user_id=row["user_id"],
                        state="creating",
                        reason=outcome.reason,
                    )
                )
            elif case.state not in ("open", "closed"):
                case.state, case.reason, case.check_at = "creating", outcome.reason, None
            state = "awaiting_support"
        else:
            content = (
                outcome.result
                if outcome.kind == "answered"
                else RagResult(answer_text=outcome.text, sources=[])
            )
            await _delivery(
                session,
                user_id=row["user_id"],
                chat_id=row["chat_id"],
                request_id=attempt.request_id,
                purpose="answer" if outcome.kind == "answered" else "notice",
                content=content,
                terminal=True,
                key="terminal:" + str(attempt.request_id),
            )
            state = "awaiting_delivery"
        await _execute(
            session,
            update(_Request)
            .where(_Request.request_id == attempt.request_id)
            .values(
                state=state, result=_data(_json(outcome)), pinned_generation=None, resume_at=None
            ),
        )
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
    async with _transaction() as session:
        row = await _question_row(session, attempt.request_id)
        if not _valid_attempt(row, attempt, "processing", await _now(session)):
            return False
        await _execute(
            session,
            update(_Request)
            .where(_Request.request_id == attempt.request_id)
            .values(state="queued", resume_at=retry_at),
        )
        await _outbox(session, "rag.questions", attempt.request_id, retry_at)
        return True
