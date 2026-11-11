"""Contracts: docs/documents/telegram/module/entrypoints.md."""


from collections.abc import Mapping

from rag.contracts import ConsumeDecision, RagResult, RequestRef
from rag.rag_api.contracts import IntakeResult, SendResult


async def handle_telegram_update(update: Mapping[str, object]) -> IntakeResult:
    """Проверить Telegram update и идемпотентно зарегистрировать вопрос с outbox.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        update: Декодированный объект Telegram Update.

    Returns:
        IntakeResult: accepted/duplicate с request_id либо ignored/invalid_update/retry_later.
    """
    raise NotImplementedError


async def handle_rag_result(event: RequestRef) -> ConsumeDecision:
    """Взять готовый ответ из PostgreSQL и провести доставку в Telegram.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        event: Ссылка на строку запроса в PostgreSQL.

    Returns:
        ConsumeDecision для адаптера RabbitMQ; результат отправки фиксируется в БД.
    """
    raise NotImplementedError


async def send_answer(chat_id: int, result: RagResult) -> SendResult:
    """Оформить и отправить одно сообщение с учётом лимитов Telegram.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        chat_id: Чат из ClaimedDelivery.
        result: Сохранённые текст и список источников из ClaimedDelivery.

    Returns:
        SendResult: sent после подтверждения или failed с признаками повтора
        и неизвестного исхода. При превышении длины отправляется короткий резервный текст.
    """
    raise NotImplementedError
