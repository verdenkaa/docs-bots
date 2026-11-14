"""Contracts: docs/documents/telegram/module/entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from collections.abc import Mapping

from rag.contracts import ConsumeDecision, DeliveryRef
from rag.rag_api.contracts import ClaimedDelivery, DeliveryPart, IntakeResult, SendResult


async def handle_telegram_update(update: Mapping[str, object]) -> IntakeResult:
    """Проверить Telegram update и идемпотентно зарегистрировать вопрос с outbox.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        update: Декодированный Telegram Update.

    Returns:
        Один IntakeResult; поля и инварианты — в [DTO](./contracts.md#приём).
    """
    raise NotImplementedError


async def handle_rag_result(event: DeliveryRef) -> ConsumeDecision:
    """Доставить оставшиеся части сохранённого плана по DeliveryRef без повторного Core.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        event: Вход по контракту владельца.

    Returns:
        Одна строка Python.
    """
    raise NotImplementedError


def build_delivery_parts(delivery: ClaimedDelivery, source_base_url: str) -> list[DeliveryPart]:
    """Собрать неизменяемый план текста, источников, кнопок и изображений без ввода-вывода.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        delivery: Контент/состояние и назначение из PostgreSQL.
        source_base_url: Проверенный адрес RAG API из конфигурации для ссылок источников.

    Returns:
        Непустой упорядоченный список TextPart/ImagePart.
    """
    raise NotImplementedError


async def send_answer(chat_id: int, part: DeliveryPart) -> SendResult:
    """Отправить одну TextPart или ImagePart и вернуть квитанцию либо типизированный сбой.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        chat_id: Личный чат из ClaimedDelivery.
        part: Одна неизменяемая часть, не весь RagResult.

    Returns:
        Один вариант SendResult.
    """
    raise NotImplementedError


async def run_api_maintenance(limit: int) -> int:
    """Запустить ограниченный проход статусов, поддержки и восстановления доставок.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        limit: Максимальная пачка для каждого фонового прохода.

    Returns:
        Целое число.
    """
    raise NotImplementedError
