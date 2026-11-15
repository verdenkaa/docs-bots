"""Contracts: docs/documents/telegram/mini-app-contracts.md."""

from typing import Annotated, Literal
from pydantic import Field, StringConstraints
from rag.dto import DTO, PositiveId


class MiniAppIdentity(DTO):
    """Identity constructed exclusively after verification of Telegram initData."""

    user_id: PositiveId = Field(
        description="Telegram User ID из проверенного initData; владелец веб-операций."
    )
    username: str | None = Field(
        default=None, description="Нормализованный проверенный username для допуска."
    )
    role: Literal["user", "admin"] = Field(
        default="user",
        description="Серверная роль по списку Telegram ID, не из JSON клиента.",
    )


class WebQuestionInput(DTO):
    """Only text is accepted; uploads are deliberately not part of this contract."""

    text: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=16000)
    ] = Field(
        description="Самостоятельный текстовый вопрос без контекста; загрузки не принимаются."
    )


class WebSupportInput(DTO):
    reason: Literal["dissatisfied", "human_request"] = Field(
        description="Причина запуска ручного ожидания поддержки."
    )


class WebAccessInput(DTO):
    enabled: bool = Field(
        strict=True,
        description="Новый допуск username; административную роль не назначает.",
    )
