"""Contracts: docs/documents/infrastructure/media.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from uuid import UUID

from pydantic import Field

from rag.contracts import ImageContent
from rag.dto import DTO, NonEmptyText


class SourceMetadata(DTO):
    """Сохранённое название документа для HTML-заглушки источника."""
    document_id: UUID = Field(description='Стабильный UUID документа, общий для его редакций.')
    source_name: NonEmptyText = Field(description='Сохранённое название исходника для ответа и HTML-заглушки.')


async def get_question_image(file_id: str) -> ImageContent:
    """Получить проверенные байты изображения по Telegram file_id для OCR.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        file_id: Telegram file_id из сохранённого входа.

    Returns:
        ImageContent.
    """
    raise NotImplementedError


async def load_source_metadata(document_id: UUID) -> SourceMetadata | None:
    """Прочитать сохранённое название документа для HTML-заглушки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        document_id: Из параметра пути источника.

    Returns:
        SourceMetadata или None.
    """
    raise NotImplementedError
