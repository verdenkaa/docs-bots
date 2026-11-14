"""Contracts: docs/documents/infrastructure/media.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from rag.contracts import ImageContent
from rag.dto import DTO, NonEmptyText

import asyncio
from urllib.parse import quote
import httpx
from rag.contracts import StepError
from rag.runtime._state import _get
from rag.runtime._validation import _image, _text
from rag.runtime._http import _read
from rag.runtime.postgresql._db import _transaction, _one

from sqlalchemy import select
from rag.runtime.postgresql._models import _Revision


class SourceMetadata(DTO):
    """Сохранённое название документа для HTML-заглушки источника."""

    document_id: UUID = Field(description="Стабильный UUID документа, общий для его редакций.")
    source_name: NonEmptyText = Field(
        description="Сохранённое название исходника для ответа и HTML-заглушки."
    )


async def get_question_image(file_id: str) -> ImageContent:
    """Получить проверенные байты изображения по Telegram file_id для OCR.

    Args:
        file_id: Telegram file_id из сохранённого входа.

    Returns:
        ImageContent.
    """
    _text(file_id)
    state = _get()
    token = state.settings.telegram_bot_token.get_secret_value()
    if not token:
        raise StepError("telegram_not_configured", False)
    base = state.settings.telegram_base_url.rstrip("/")
    try:
        async with asyncio.timeout(state.settings.io_timeout_s):
            response = await state.http.post(
                base + "/bot" + token + "/getFile", json={"file_id": file_id}
            )
            if response.status_code != 200:
                raise StepError(
                    "telegram_file_unavailable",
                    response.status_code == 429 or response.status_code >= 500,
                )
            payload = response.json()
            if (
                not isinstance(payload, dict)
                or payload.get("ok") is not True
                or not isinstance(payload.get("result"), dict)
            ):
                code = payload.get("error_code", 0) if isinstance(payload, dict) else 0
                raise StepError("telegram_invalid_file_response", code == 429 or code >= 500)
            result = payload["result"]
            path = result.get("file_path")
            if (
                not isinstance(path, str)
                or not path
                or path.startswith("/")
                or any(part in (".", "..") for part in path.split("/"))
                or any(character in path for character in ("\\", "?", ":", "#", "\n", "\r"))
            ):
                raise StepError("telegram_invalid_file_path", False)
            if (
                type(result.get("file_size")) is int
                and result["file_size"] > state.settings.max_image_bytes
            ):
                raise StepError("image_too_large", False)
            async with state.http.stream(
                "GET", base + "/file/bot" + token + "/" + quote(path, safe="/")
            ) as download:
                if download.status_code != 200:
                    raise StepError(
                        "telegram_file_unavailable",
                        download.status_code == 429 or download.status_code >= 500,
                    )
                return _image(await _read(download, state.settings.max_image_bytes))
    except (httpx.HTTPError, OSError, TimeoutError):
        raise StepError("telegram_file_unavailable", True) from None
    except (ValueError, TypeError) as exc:
        raise StepError("telegram_invalid_file_response", False) from exc


async def load_source_metadata(document_id: UUID) -> SourceMetadata | None:
    """Прочитать сохранённое название документа для HTML-заглушки.

    Args:
        document_id: Из параметра пути источника.

    Returns:
        SourceMetadata или None.
    """
    async with _transaction() as connection:
        row = await _one(
            connection,
            select(_Revision.document_id, _Revision.source_name)
            .where(_Revision.document_id == document_id, _Revision.visible_from.is_not(None))
            .order_by(_Revision.visible_from.desc())
            .limit(1),
        )
        return SourceMetadata(**row) if row else None


async def prepare_telegram_photo(image: ImageContent) -> ImageContent:
    """Подготовить копию изображения под ограничения Telegram sendPhoto.

    Runtime вне event loop нормализует ориентацию, RGB/JPEG, размеры,
    пропорции и объём; S3-оригинал не изменяется. Непреобразуемый вход
    даёт постоянный StepError до сетевой отправки.

    Args:
        image: Проверенные байты изображения, полученные через get_image.

    Returns:
        ImageContent с MIME image/jpeg и байтами, допустимыми для sendPhoto.
    """
    from rag.runtime._photos import _prepare_photo

    return await _prepare_photo(image)
