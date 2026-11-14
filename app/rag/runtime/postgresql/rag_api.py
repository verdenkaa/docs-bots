"""Contracts: docs/documents/infrastructure/postgresql/rag-api.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Literal

from rag.contracts import DeliveryAttempt
from rag.rag_api.contracts import ClaimedDelivery, DeliveryPart, IntakeResult, NewQuestion


async def register_question(question: NewQuestion) -> IntakeResult:
    """Атомарно проверить повтор, допуск и активный слот; сохранить вопрос и outbox.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        question: Проверенный личный чат, отправитель, вход и время.

    Returns:
        IntakeResult.
    """
    raise NotImplementedError


async def record_update(update_id: int, user_id: int | None, chat_id: int | None, received_at: datetime, kind: Literal['ignored', 'denied', 'invalid_update']) -> IntakeResult:
    """Сохранить обработанный update и окончательный исход без нового RAG-вопроса.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        update_id: Идентичность и время обработанного update.
        user_id: Получатель, если достоверно известен личный чат.
        chat_id: Получатель, если достоверно известен личный чат.
        received_at: Идентичность и время обработанного update.
        kind: Выбранный RAG API окончательный исход.

    Returns:
        IntakeResult.
    """
    raise NotImplementedError


async def claim_delivery(delivery_id: int) -> ClaimedDelivery | None:
    """Взять готовую самостоятельную доставку и выдать отдельную DeliveryAttempt.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        delivery_id: Ссылка из DeliveryRef.

    Returns:
        ClaimedDelivery или None.
    """
    raise NotImplementedError


async def store_delivery_parts(attempt: DeliveryAttempt, parts: Sequence[DeliveryPart]) -> bool:
    """Закрепить неизменяемый план частей перед первой отправкой Telegram.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Взятая попытка.
        parts: Уже собранный неизменяемый план.

    Returns:
        bool.
    """
    raise NotImplementedError


async def record_delivery_part(attempt: DeliveryAttempt, part_no: int, message_id: int, variant: Literal['normal', 'length_fallback']) -> bool:
    """Сохранить квитанцию одной части и перейти к следующей неподтверждённой.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Право и индекс текущей части.
        part_no: Право и индекс текущей части.
        message_id: Реальная квитанция Telegram.
        variant: Реальная квитанция Telegram.

    Returns:
        bool.
    """
    raise NotImplementedError


async def schedule_delivery_retry(attempt: DeliveryAttempt, retry_at: datetime, uncertain: bool) -> bool:
    """Сохранить время повтора доставки и outbox rag.results.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Текущая попытка доставки.
        retry_at: Следующий запуск и возможная отправка текущей части.
        uncertain: Следующий запуск и возможная отправка текущей части.

    Returns:
        bool.
    """
    raise NotImplementedError


async def complete_delivery(attempt: DeliveryAttempt, delivered: bool, uncertain: bool) -> bool:
    """Завершить доставку и разблокировать следующий ответ чата.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        attempt: Текущая попытка.
        delivered: Все части подтверждены / хотя бы одна могла дойти.
        uncertain: Все части подтверждены / хотя бы одна могла дойти.

    Returns:
        bool.
    """
    raise NotImplementedError


async def queue_waiting_statuses(now: datetime, limit: int) -> int:
    """Поставить актуальные статусы после 10 секунд ожидания без готового итога.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        now: Момент и предел пачки.
        limit: Момент и предел пачки.

    Returns:
        int.
    """
    raise NotImplementedError
