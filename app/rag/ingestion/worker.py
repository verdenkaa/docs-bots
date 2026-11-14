"""Contracts: docs/documents/ingestion/entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from rag.contracts import ConsumeDecision
from rag.ingestion.contracts import JobRef, PreparedChunk, StagedUpload


async def handle_ingestion_job(event: JobRef) -> ConsumeDecision:
    """Провести задание через парсинг, эмбеддинг, запись и публикацию редакции.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        event: Ссылка на задание, не файл.

    Returns:
        Одна строка ConsumeDecision.
    """
    raise NotImplementedError


async def prepare_chunks(upload: StagedUpload) -> list[PreparedChunk]:
    """Нарезать готовый Markdown на чанки без разрыва плейсхолдеров.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        upload: Markdown и манифест из Runtime.

    Returns:
        Упорядоченный список PreparedChunk; [] — нет пригодного содержимого.
    """
    raise NotImplementedError
