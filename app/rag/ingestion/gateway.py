"""Contracts: docs/documents/ingestion/entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from rag.ingestion.contracts import UploadInput, UploadResult


async def accept_upload(upload: UploadInput) -> UploadResult:
    """Конвертировать документ, подтвердить S3/Markdown и зарегистрировать задание с outbox.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        upload: Поток и метаданные.

    Returns:
        Один вариант Pydantic UploadResult.
    """
    raise NotImplementedError
