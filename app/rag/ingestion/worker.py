"""Contracts: docs/documents/ingestion/entrypoints.md.

Индексатор готовой партии. Подготовка чанков принадлежит входной стороне Gateway.
"""


from __future__ import annotations

from rag.contracts import ConsumeDecision
from rag.ingestion._dependencies import _Dependencies
from rag.ingestion._indexing import _Indexer
from rag.ingestion._settings import _Settings
from rag.ingestion.contracts import JobRef, PreparedChunk, StagedUpload


async def handle_ingestion_job(event: JobRef) -> ConsumeDecision:
    """Загрузить готовую партию чанков, закрепить версию, записать и опубликовать оба индекса.

    Args:
        event: Ссылка на задание, не файл.

    Returns:
        Одна строка ConsumeDecision.
    """
    event = JobRef.model_validate(event.model_dump() if isinstance(event, JobRef) else event)
    return await _Indexer(_Settings.from_environment(), _Dependencies()).handle(event)


async def prepare_chunks(upload: StagedUpload) -> list[PreparedChunk]:
    """Подготовить чанки для Gateway до регистрации задания; индексатор разбиение не повторяет.

    Args:
        upload: Markdown и манифест из Runtime.

    Returns:
        Упорядоченный список PreparedChunk; [] — нет пригодного содержимого.
    """
    from rag.ingestion._chunking import _prepare_chunks
    return await _prepare_chunks(upload)
