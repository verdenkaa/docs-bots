"""Contracts: docs/documents/infrastructure/s3.md.

Адаптер S3 для записи изображений Ingestion и чтения при доставке RAG API.
Клиент создаётся при явном запуске Runtime.
"""

from __future__ import annotations

from uuid import UUID

from rag.contracts import ImageContent

from rag.contracts import StepError
from rag.runtime._state import _get
from rag.runtime._validation import _image
from rag.runtime._http import _s3, _s3_status


async def get_image(image_id: UUID) -> ImageContent:
    """Получить изображение настроенного S3-bucket по стабильному UUID.

    Args:
        image_id: Разрешённый ID сохранённого ответа.

    Returns:
        ImageContent.
    """
    if not isinstance(image_id,UUID):
        raise ValueError("Expected an image UUID")
    status, headers, data = await _s3("GET","images/"+str(image_id))
    _s3_status(status)
    return _image(data,headers.get("content-type"))


async def put_image(image_id: UUID, image: ImageContent) -> None:
    """Сохранить извлечённое изображение без перезаписи другого содержимого под тем же ID.

    Args:
        image_id: ID, который Gateway помещает в плейсхолдер и ImageRef.
        image: Байты и MIME извлечённого изображения.

    Returns:
        None.
    """
    if not isinstance(image_id,UUID):
        raise ValueError("Expected an image UUID")
    image = ImageContent.model_validate(image.model_dump())
    if len(image.data)>_get().settings.max_image_bytes:
        raise StepError("image_too_large",False)
    _image(image.data,image.mime_type)
    failure = None
    try:
        status, _, _ = await _s3("PUT","images/"+str(image_id),image.data,
            {"content-type":image.mime_type,"if-none-match":"*"})
    except StepError as exc:
        if not exc.retryable:
            raise
        failure = exc
        status = 0
    if status in (409,412) or failure is not None:
        try:
            existing = await get_image(image_id)
        except StepError:
            if failure is not None:
                raise failure
            raise
        if existing.data!=image.data or existing.mime_type!=image.mime_type:
            raise StepError("image_conflict",False)
        return
    _s3_status(status)
