"""Contracts: docs/documents/infrastructure/s3.md.

Адаптер S3 для записи изображений Ingestion и чтения при доставке RAG API.
Прикладные операции пока являются заглушками; клиент собирает Runtime.
"""

from __future__ import annotations

from uuid import UUID

from rag.contracts import ImageContent


async def get_image(image_id: UUID) -> ImageContent:
    """Получить изображение настроенного S3-bucket по стабильному UUID.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        image_id: Разрешённый ID сохранённого ответа.

    Returns:
        ImageContent.
    """
    raise NotImplementedError


async def put_image(image_id: UUID, image: ImageContent) -> None:
    """Сохранить извлечённое изображение без перезаписи другого содержимого под тем же ID.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        image_id: ID, который Gateway помещает в плейсхолдер и ImageRef.
        image: Байты и MIME извлечённого изображения.

    Returns:
        None.
    """
    raise NotImplementedError
