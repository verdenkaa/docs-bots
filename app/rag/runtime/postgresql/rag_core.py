"""Contracts: docs/documents/infrastructure/postgresql/rag-core.md."""


from datetime import datetime
from uuid import UUID

from rag.contracts import ClaimedQuestion, ProcessingAttempt, RagResult


async def claim_question(request_id: int) -> ClaimedQuestion | None:
    """Атомарно взять первый доступный вопрос чата и закрепить поколение корпуса.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        request_id: Ссылка из уведомления RabbitMQ на сохранённый запрос.

    Returns:
        ClaimedQuestion либо None: нет, занят, завершён или пока не готов.
        Повтор использует ранее закреплённое поколение.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def load_revision_ids(corpus_generation: int) -> frozenset[UUID]:
    """Получить редакции, видимые в закреплённом поколении корпуса.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        corpus_generation: Поколение, закреплённое в PostgreSQL за этим запросом.

    Returns:
        Неизменяемое множество UUID; пустое множество допустимо.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def complete_question(attempt: ProcessingAttempt, result: RagResult) -> bool:
    """Сохранить результат, освободить закрепление и создать outbox rag.results.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        attempt: Активная попытка, полученная при взятии работы.
        result: Готовый текст и источники.

    Returns:
        True — всё сохранено одной транзакцией; False — попытка утратила право записи.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError


async def schedule_question_retry(attempt: ProcessingAttempt, retry_at: datetime) -> bool:
    """Сохранить отложенный повтор обработки с outbox rag.questions.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        attempt: Активная попытка, полученная при взятии работы.
        retry_at: Время, с которого можно повторить работу. Дата и время в UTC.

    Returns:
        True — повтор сохранён; False — попытка утратила право записи.
        Поколение корпуса остаётся закреплённым.

    Raises:
        StorageError: БД недоступна или исход коммита неизвестен.
    """
    raise NotImplementedError
