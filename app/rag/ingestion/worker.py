"""Contracts: docs/documents/ingestion/entrypoints.md."""


from rag.contracts import ConsumeDecision
from rag.ingestion.contracts import JobRef, PreparedChunk, StagedUpload


async def handle_ingestion_job(event: JobRef) -> ConsumeDecision:
    """Провести задание через парсинг, эмбеддинг, запись и публикацию редакции.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        event: Ссылка на сохранённое задание.

    Returns:
        ConsumeDecision: ack после устойчивого исхода, retry_broker при неизвестном
        переходе в PostgreSQL. Возврат сам по себе не отправляет AMQP ACK.
    """
    raise NotImplementedError


async def prepare_chunks(upload: StagedUpload) -> list[PreparedChunk]:
    """Распарсить временный исходник и нарезать его на чанки.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        upload: Путь к сохранённому файлу и его метаданные.

    Returns:
        Список PreparedChunk в порядке будущих chunk_no, без эмбеддингов.
    """
    raise NotImplementedError
