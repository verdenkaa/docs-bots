"""Общая строгая валидация DTO; технические потоки не являются JSON-данными."""

import re
from typing import Annotated
from urllib.parse import urlsplit
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class DTO(BaseModel):
    """Базовый DTO: строгие типы, запрет лишних полей и нечисловых float.

    Валидацию на границе вызывает адаптер; аннотация функции сама её не запускает.
    """

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


PositiveId = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]


def _check_source_url(value: str | None) -> str | None:
    """Сохранить исходный HTTP(S) URL без нормализации и встроенных credentials."""
    if value is None:
        return None
    parsed = urlsplit(value)
    parsed.port
    if (parsed.scheme not in ("https", "http") or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or any(char.isspace() or ord(char) < 32 for char in value)):
        raise ValueError("source_url должен быть HTTP(S) URL без credentials и пробелов")
    return value


def _check_image_markers(text: str, image_ids: list[UUID]) -> None:
    """Проверить UUID служебных плейсхолдеров и уникальность списка изображений."""
    if len(image_ids) != len(set(image_ids)):
        raise ValueError("ID изображений не должны повторяться")
    markers = re.findall(r"!\[(?:\\.|[^\]\\])*\]\(image://([^)]+)\)", text)
    if text.count("image://") != len(markers):
        raise ValueError("Некорректный плейсхолдер изображения")
    if {UUID(value) for value in markers} != set(image_ids):
        raise ValueError("Плейсхолдеры и список изображений не совпадают")
