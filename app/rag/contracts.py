"""Contracts: docs/documents/rag-core/contracts.md."""

from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText


from typing import Annotated, Literal
from uuid import UUID


class RequestRef(DTO):
    """Ссылка из очереди на вопрос в PostgreSQL; сам текст в очередь не копируется."""

    request_id: PositiveId = Field(description="ID сохранённого вопроса в PostgreSQL; связывает обработку и доставку.")


# Решение для AMQP-адаптера: подтвердить доставку или вернуть её брокеру.
# Это не результат ответа пользователю и не непосредственный вызов ACK/NACK.
ConsumeDecision = Literal["ack", "retry_broker"]


class ProcessingAttempt(DTO):
    """Право текущей попытки обработки или доставки изменять состояние вопроса.

    Переходы проверяют номер и срок в PostgreSQL; истечение срока не останавливает вычисление.
    """

    request_id: PositiveId = Field(description="ID сохранённого вопроса в PostgreSQL; связывает обработку и доставку.")
    attempt_no: PositiveId = Field(
        description="Номер попытки текущего этапа; защищает от записи результата старой попыткой.",
    )
    deadline_at: AwareDatetime = Field(
        description="Срок действия попытки, datetime с часовым поясом; передаётся в UTC.",
    )


class ClaimedQuestion(DTO):
    """Вопрос, взятый Core в работу, с закреплённым поколением корпуса."""

    attempt: ProcessingAttempt = Field(
        description="Активная попытка, полученная при атомарном взятии работы в PostgreSQL.",
    )
    raw_question: str = Field(
        description="Исходный текст пользователя до Guardrails; пустота проверяется бизнес-логикой.",
    )
    corpus_generation: NonNegativeInt = Field(
        description="Поколение набора доступных редакций; 0 допустим для пустого корпуса.",
    )


class SourceLocator(DTO):
    """Положение фрагмента внутри документа; неизвестные координаты можно опустить."""

    page: PositiveId | None = Field(
        description="Номер страницы с 1; None или отсутствие поля означает неизвестную страницу.", default=None,
    )
    section: NonEmptyText | None = Field(
        description="Заголовок или путь раздела; None или отсутствие поля — положение неизвестно.", default=None,
    )


class SourceRef(DTO):
    """Метаданные цитируемого чанка для ответа; полный текст сюда не входит."""

    chunk_id: UUID = Field(description="Детерминированный UUID точки: UUIDv5 от revision_id и chunk_no.")
    document_id: UUID = Field(description="Стабильный ID документа, общий для всех его редакций.")
    revision_id: UUID = Field(description="ID конкретной редакции документа; совпадает с job_id индексации.")
    source_name: NonEmptyText = Field(description="Читаемое название источника для оформления ответа.")
    locator: SourceLocator | None = Field(
        description="Положение в документе; None, если парсер не смог его определить.",
    )
    source_url: str | None = Field(description="Исходная ссылка на документ; None, если её не предоставили.")


class SearchFragment(SourceRef):
    """Найденный чанк: текст для генерации и унаследованные метаданные источника."""

    text: NonEmptyText = Field(description="Текст фрагмента для последующей обработки.")


class RagResult(DTO):
    """Готовый пользовательский ответ с источниками для сохранения и доставки."""

    answer_text: NonEmptyText = Field(description="Готовый текст ответа пользователю до Telegram-оформления.")
    sources: list[SourceRef] = Field(
        description="Метаданные источников ответа; пустой список допустим для служебного ответа.",
    )


class AllowedQuestion(DTO):
    """Guardrails разрешил вопрос и вернул нормализованный текст для следующих шагов."""

    kind: Literal["allowed"] = Field(description="Метка исхода; определяет вариант DTO при разборе объединения.")
    normalized_question: NonEmptyText = Field(
        description="Нормализованный разрешённый вопрос; переиспользуется кешем и поиском.",
    )


class RejectedQuestion(DTO):
    """Guardrails отклонил инъекцию или некорректный ввод; дальнейший поиск не нужен."""

    kind: Literal["blocked", "invalid_input"] = Field(
        description="Метка исхода; определяет вариант DTO при разборе объединения.",
    )


class DetectorUnavailable(DTO):
    """Проверку защиты выполнить не удалось; это технический исход, а не разрешение."""

    kind: Literal["detector_unavailable"] = Field(
        description="Метка исхода; определяет вариант DTO при разборе объединения.",
    )
    retryable: bool = Field(description="Можно ли повторить операцию по правилам владельца этапа.")


GuardrailsResult = Annotated[AllowedQuestion | RejectedQuestion | DetectorUnavailable, Field(discriminator="kind")]


class GeneratedAnswer(DTO):
    """Ответ по документам с ID процитированных чанков; Core собирает SourceRef."""

    kind: Literal["answered"] = Field(description="Метка исхода; определяет вариант DTO при разборе объединения.")
    answer_text: NonEmptyText = Field(description="Готовый текст ответа пользователю до Telegram-оформления.")
    cited_chunk_ids: Annotated[list[UUID], Field(min_length=1)] = Field(
        description="Непустой список ID процитированных чанков из переданного контекста.",
    )


class InsufficientEvidence(DTO):
    """Генератор не нашёл достаточных оснований для ответа в переданных фрагментах."""

    kind: Literal["insufficient_evidence"] = Field(
        description="Метка исхода; определяет вариант DTO при разборе объединения.",
    )


GenerationResult = Annotated[GeneratedAnswer | InsufficientEvidence, Field(discriminator="kind")]


class StepError(Exception):
    """Ошибка шага пайплайна с кодом для диагностики и признаком допустимости повтора."""

    def __init__(self, code: str, retryable: bool) -> None:
        """Сохранить код ошибки и признак допустимости повтора шага."""
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class StorageError(Exception):
    """Операция хранилища не подтверждена; коммит мог состояться.

    Нельзя считать это доказательством отсутствия записи.
    """
