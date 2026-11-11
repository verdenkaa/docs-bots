"""Shared validation policy for transport DTOs (not services or file streams)."""

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class DTO(BaseModel):
    """Базовый DTO: строгие типы, запрет лишних полей и нечисловых float.

    Валидацию на границе вызывает адаптер; аннотация функции сама её не запускает.
    """

    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


PositiveId = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
