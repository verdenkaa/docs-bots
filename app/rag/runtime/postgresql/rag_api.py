"""Contracts: docs/documents/infrastructure/postgresql/rag-api.md."""


from datetime import datetime

from rag.contracts import ProcessingAttempt
from rag.rag_api.contracts import ClaimedDelivery, RegisteredQuestion


async def register_question(update_id: int, chat_id: int, question_text: str) -> RegisteredQuestion | None:
    """Идемпотентно зарегистрировать Telegram update и outbox rag.questions.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        update_id: Telegram Update ID для дедупликации.
        chat_id: Чат для доставки и порядок вопросов.
        question_text: Исходный текст Telegram Update, который позже получит Guardrails.

    Returns:
        RegisteredQuestion для нового или уже известного update; None при достижении
        лимита незавершённых доставок. Уже известный update возвращается и при лимите.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def claim_delivery(request_id: int) -> ClaimedDelivery | None:
    """Взять первый готовый к доставке ответ чата и выдать действующую попытку.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        request_id: Ссылка из уведомления RabbitMQ на сохранённый запрос.

    Returns:
        ClaimedDelivery либо None: запись не готова, занята, завершена или ждёт очереди.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def schedule_delivery_retry(attempt: ProcessingAttempt, retry_at: datetime, uncertain: bool) -> bool:
    """Сохранить время повтора доставки и outbox rag.results.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        attempt: Активная попытка, полученная при взятии работы.
        retry_at: Время, с которого можно повторить работу. Дата и время в UTC.
        uncertain: Могла ли текущая отправка дойти без подтверждения.

    Returns:
        True — повтор сохранён; False — попытка утратила право на переход.
        uncertain сохраняет риск ранее состоявшейся отправки.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def complete_delivery(attempt: ProcessingAttempt, delivered: bool, uncertain: bool) -> bool:
    """Завершить доставку и разблокировать следующий ответ чата.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        attempt: Активная попытка, полученная при взятии работы.
        delivered: Подтвердил ли Telegram отправку.
        uncertain: Могла ли текущая отправка дойти без подтверждения.

    Returns:
        True — переход сохранён; False — попытка больше не имеет права записи.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError
