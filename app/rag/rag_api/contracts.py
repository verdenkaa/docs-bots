"""Contracts: docs/documents/telegram/module/entrypoints.md."""

from pydantic import Field
from rag.dto import DTO, PositiveId, NonNegativeInt


from typing import Annotated, Literal

from rag.contracts import ProcessingAttempt, RagResult


class RegisteredQuestion(DTO):
    """Результат идемпотентной регистрации Telegram update в PostgreSQL."""

    request_id: PositiveId = Field(description="ID сохранённого вопроса в PostgreSQL; связывает обработку и доставку.")
    created: bool = Field(description="True — вопрос создан сейчас; False — найден ранее зарегистрированный update.")


class RecordedUpdate(DTO):
    """Telegram update принят или распознан как дубль сохранённого вопроса."""

    kind: Literal["accepted", "duplicate"] = Field(
        description="Метка исхода; определяет вариант DTO при разборе объединения.",
    )
    request_id: PositiveId = Field(description="ID сохранённого вопроса в PostgreSQL; связывает обработку и доставку.")


class UnrecordedUpdate(DTO):
    """Update не зарегистрирован: не подходит, некорректен или требует повтора."""

    kind: Literal["ignored", "invalid_update", "retry_later"] = Field(
        description="Метка исхода; определяет вариант DTO при разборе объединения.",
    )


IntakeResult = Annotated[RecordedUpdate | UnrecordedUpdate, Field(discriminator="kind")]


class ClaimedDelivery(DTO):
    """Ответ, взятый API для доставки в чат в рамках действующей попытки."""

    attempt: ProcessingAttempt = Field(
        description="Активная попытка, полученная при атомарном взятии работы в PostgreSQL.",
    )
    chat_id: int = Field(description="Telegram ID чата доставки; может быть отрицательным.")
    result: RagResult = Field(description="Сохранённый ответ с источниками для пользовательской доставки.")


class Sent(DTO):
    """Telegram подтвердил отправку пользовательского сообщения."""

    kind: Literal["sent"] = Field(description="Метка исхода; определяет вариант DTO при разборе объединения.")


class SendFailed(DTO):
    """Отправка не подтверждена; признаки определяют повтор и риск дублирования."""

    kind: Literal["failed"] = Field(description="Метка исхода; определяет вариант DTO при разборе объединения.")
    retryable: bool = Field(description="Можно ли повторить операцию по правилам владельца этапа.")
    uncertain: bool = Field(description="Сообщение могло дойти без подтверждения; повтор способен создать дубль.")
    retry_after_seconds: NonNegativeInt | None = Field(
        description="Задержка повтора от Telegram в секундах; None, если не указана.",
    )


SendResult = Annotated[Sent | SendFailed, Field(discriminator="kind")]
