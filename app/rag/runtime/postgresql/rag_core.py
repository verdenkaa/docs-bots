"""Contracts: docs/documents/infrastructure/postgresql/rag-core.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from datetime import datetime
from uuid import UUID

from rag.contracts import ClaimedQuestion, ProcessingAttempt, QuestionOutcome


async def claim_question(request_id: int) -> ClaimedQuestion | None:
    """Атомарно взять первый доступный вопрос чата и закрепить поколение корпуса.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        request_id: ID сохранённого вопроса.

    Returns:
        ClaimedQuestion или None.
    """
    raise NotImplementedError


async def save_prepared_question(attempt: ProcessingAttempt, text: str) -> bool:
    """Сохранить подпись и OCR по действующей попытке для повторов после рестарта.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Действующая попытка и объединённый OCR-текст/подпись.
        text: Действующая попытка и объединённый OCR-текст/подпись.

    Returns:
        bool.
    """
    raise NotImplementedError


async def load_revision_ids(corpus_generation: int) -> frozenset[UUID]:
    """Получить редакции, видимые в закреплённом поколении корпуса.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        corpus_generation: Закреплённый состав.

    Returns:
        frozenset UUID.
    """
    raise NotImplementedError


async def complete_question(attempt: ProcessingAttempt, outcome: QuestionOutcome) -> bool:
    """Сохранить QuestionOutcome и создать доставку либо намерение Support.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Право обработки.
        outcome: AnsweredOutcome / NoticeOutcome / SupportOutcome.

    Returns:
        bool.
    """
    raise NotImplementedError


async def schedule_question_retry(attempt: ProcessingAttempt, retry_at: datetime) -> bool:
    """Сохранить отложенный повтор обработки с outbox rag.questions.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Текущая попытка и следующий запуск.
        retry_at: Текущая попытка и следующий запуск.

    Returns:
        bool.
    """
    raise NotImplementedError
