"""Contracts: docs/documents/ingestion/entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from uuid import UUID

from rag.ingestion.contracts import ConversionResult, UploadInput


async def convert_document(upload: UploadInput, revision_id: UUID) -> ConversionResult:
    """Привести документ к Markdown и извлечь изображения для сохранения Gateway в S3.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        upload: Поток читается один раз с лимитами.
        revision_id: Основа стабильных ID изображений.

    Returns:
        Один Pydantic ConversionResult, без оригинала.
    """
    raise NotImplementedError
