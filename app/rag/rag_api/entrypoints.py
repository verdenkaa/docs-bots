"""Contracts: docs/documents/telegram/module/entrypoints.md.

Прикладная координация приёма, доставки и Maintenance через Runtime.
"""


from __future__ import annotations

from collections.abc import Mapping

from rag.contracts import ConsumeDecision, DeliveryRef, StorageError
from rag.rag_api import intake, support
from rag.rag_api.config import get_settings
from rag.rag_api.delivery import formatter, sender
from rag.runtime.postgresql import rag_api as repository
from rag.rag_api._observability import _operation, _step, _call, _part_scope, _utc_now
from datetime import datetime, timedelta, timezone
import asyncio
from rag.rag_api.contracts import ClaimedDelivery, DeliveryPart, IntakeResult, SendResult


async def handle_telegram_update(update: Mapping[str, object]) -> IntakeResult:
    """Проверить Telegram update и идемпотентно зарегистрировать вопрос с outbox.

    Args:
        update: Декодированный Telegram Update.

    Returns:
        Один IntakeResult; поля и инварианты — в [DTO](./contracts.md#приём).
    """
    with _operation("handle_telegram_update", received_at=_utc_now()) as trace:
        if isinstance(update, Mapping) and type(update.get("update_id")) is int:
            trace.fields["update_id"] = update["update_id"]
        try:
            result = await intake._handle(update)
        except StorageError:
            trace.fields["error_code"] = "intake_storage_error"
            result = IntakeResult(kind="retry_later")
        trace.fields.update(outcome=result.kind, request_id=result.request_id,
                            delivery_id=result.delivery_id,
                            case_id=str(result.case_id) if result.case_id else None)
        return result


async def handle_rag_result(event: DeliveryRef) -> ConsumeDecision:
    """Доставить оставшиеся части сохранённого плана по DeliveryRef без повторного Core.

    Args:
        event: Вход по контракту владельца.

    Returns:
        Одна строка Python.
    """
    event = DeliveryRef.model_validate(event.model_dump())
    with _operation("handle_rag_result", started=True, delivery_id=event.delivery_id) as trace:
        decision, outcome, error_code = "retry_broker", "error", None
        try:
            delivery = await _call("claim_delivery", repository.claim_delivery, event.delivery_id)
            if delivery is None:
                decision, outcome = "ack", "not_required"
                return decision
            attempt = delivery.attempt
            trace.fields.update(request_id=delivery.request_id, purpose=delivery.purpose,
                                attempt_no=attempt.attempt_no)
            parts = delivery.parts
            fully_sent = bool(parts) and delivery.next_part == len(parts)
            if attempt.attempt_no > 5 and not fully_sent:
                saved = await _call("complete_delivery", repository.complete_delivery, attempt, False, False)
                decision, outcome = "ack", "retry_limit" if saved else "stale_attempt"
                return decision
            if parts is None:
                with _step("build_delivery_parts", error_code="delivery_plan_invalid"):
                    parts = build_delivery_parts(delivery, get_settings().public_url)
                if not await _call("store_delivery_parts", repository.store_delivery_parts, attempt, parts):
                    decision, outcome = "ack", "stale_attempt"
                    return decision
            for index in range(delivery.next_part, len(parts)):
                remaining = (attempt.deadline_at - datetime.now(timezone.utc)).total_seconds()
                if remaining <= 0:
                    outcome, error_code = "lease_expired", "delivery_deadline"
                    return decision
                with _part_scope(part_no=index, delivery_id=event.delivery_id, purpose=delivery.purpose):
                    try:
                        with _step("send_part", error_code="telegram_send_failed") as step:
                            async with asyncio.timeout(remaining):
                                result = await send_answer(delivery.chat_id, parts[index])
                            step["outcome"] = result.kind
                            if result.kind == "sent":
                                step.update(message_id=result.message_id, variant=result.variant)
                            else:
                                step.update(uncertain=result.uncertain, retryable=result.retryable,
                                            retry_after_seconds=result.retry_after_seconds,
                                            error_code="telegram_retryable" if result.retryable else "telegram_permanent")
                    except TimeoutError:
                        outcome, error_code = "uncertain", "delivery_deadline"
                        return decision
                    if result.kind == "sent":
                        if not await _call("record_delivery_part", repository.record_delivery_part,
                                           attempt, index, result.message_id, result.variant,
                                           result.delivered_text,
                                           _fields={"message_id": result.message_id, "variant": result.variant}):
                            decision, outcome = "ack", "stale_attempt"
                            return decision
                    else:
                        if result.retryable and attempt.attempt_no < 5:
                            delay = max(min(2 ** attempt.attempt_no, 60), result.retry_after_seconds or 0)
                            retry_at = datetime.now(timezone.utc) + timedelta(seconds=delay)
                            saved = await _call("schedule_delivery_retry", repository.schedule_delivery_retry,
                                                attempt, retry_at, result.uncertain,
                                                _fields={"retry_at": retry_at.isoformat(), "uncertain": result.uncertain})
                            outcome = "retry_scheduled" if saved else "stale_attempt"
                        else:
                            saved = await _call("complete_delivery", repository.complete_delivery,
                                                attempt, False, result.uncertain,
                                                _fields={"delivered": False, "uncertain": result.uncertain})
                            outcome = ("unknown" if result.uncertain else "failed") if saved else "stale_attempt"
                        decision = "ack"
                        return decision
            saved = await _call("complete_delivery", repository.complete_delivery, attempt, True, False,
                                _fields={"delivered": True, "uncertain": False})
            decision, outcome = "ack", "delivered" if saved else "stale_attempt"
            return decision
        except StorageError:
            error_code, outcome = "delivery_storage_error", "storage_unknown"
            return decision
        except Exception:
            error_code = "delivery_internal_error"
            raise
        finally:
            trace.fields.update(outcome=outcome, error_code=error_code, consume_decision=decision)


def build_delivery_parts(delivery: ClaimedDelivery, source_base_url: str) -> list[DeliveryPart]:
    """Собрать неизменяемый план текста, источников, кнопок и изображений без ввода-вывода.

    Args:
        delivery: Контент/состояние и назначение из PostgreSQL.
        source_base_url: Проверенный адрес RAG API из конфигурации для ссылок источников.

    Returns:
        Непустой упорядоченный список TextPart/ImagePart.
    """
    return formatter._build(delivery, source_base_url)


async def send_answer(chat_id: int, part: DeliveryPart) -> SendResult:
    """Отправить одну TextPart или ImagePart и вернуть квитанцию либо типизированный сбой.

    Args:
        chat_id: Личный чат из ClaimedDelivery.
        part: Одна неизменяемая часть, не весь RagResult.

    Returns:
        Один вариант SendResult.
    """
    return await sender._send(chat_id, part)


async def run_api_maintenance(limit: int) -> int:
    """Запустить ограниченный проход статусов, поддержки и восстановления доставок.

    Args:
        limit: Максимальная пачка для каждого фонового прохода.

    Returns:
        Целое число.
    """
    if type(limit) is not int or limit <= 0:
        raise ValueError("limit must be positive")
    with _operation("run_api_maintenance") as trace:
        statuses = await _call("queue_waiting_statuses", repository.queue_waiting_statuses, datetime.now(timezone.utc), limit)
        count = statuses + await support.run_support_batch(limit)
        trace.fields.update(outcome="completed", transitions=count)
        return count
