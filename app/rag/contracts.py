"""Contracts: docs/documents/rag-core/contracts.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from rag.dto import DTO, NonEmptyText, NonNegativeInt, PositiveId, _check_image_markers


ConsumeDecision = Literal['ack', 'retry_broker']


class RequestRef(DTO):
    """Ссылка из очереди на вопрос в PostgreSQL; сам текст в очередь не копируется."""
    request_id: PositiveId = Field(description='ID принятого вопроса; не заменяет delivery_id.')


class DeliveryRef(DTO):
    """Ссылка на самостоятельную доставку; не является ID вопроса."""
    delivery_id: PositiveId = Field(description='ID самостоятельной доставки в rag_deliveries.')


class ProcessingAttempt(DTO):
    """Номер и срок попытки Core; доставка имеет отдельную DeliveryAttempt."""
    request_id: PositiveId = Field(description='ID принятого вопроса; не заменяет delivery_id.')
    attempt_no: PositiveId = Field(description='Номер попытки текущего этапа; защищает от записи результата старой попыткой.')
    deadline_at: AwareDatetime = Field(description='Срок действия попытки, datetime с часовым поясом; передаётся в UTC.')


class DeliveryAttempt(DTO):
    """Право попытки изменять состояние самостоятельной доставки."""
    delivery_id: PositiveId = Field(description='ID самостоятельной доставки в rag_deliveries.')
    attempt_no: PositiveId = Field(description='Номер попытки конкретного этапа; защищает переход от устаревшего исполнителя.')
    deadline_at: AwareDatetime = Field(description='Технический срок попытки в UTC; не целевая метрика 20 секунд.')


class TextQuestion(DTO):
    """Самостоятельный текстовый вопрос сотрудника."""
    kind: Literal['text'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    text: NonEmptyText = Field(description='Исходный самостоятельный вопрос сотрудника.')


class ImageQuestion(DTO):
    """Telegram file_id и подпись; Core извлекает только текст через OCR."""
    kind: Literal['image'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    file_id: NonEmptyText = Field(description='Устойчивая ссылка Telegram для получения изображения после рестарта.')
    caption: str | None = Field(default=None, description='Подпись пользователя, объединяемая Core с OCR без изменения смысла.')


QuestionInput = Annotated[TextQuestion | ImageQuestion, Field(discriminator='kind')]


class ClaimedQuestion(DTO):
    """Взятый вопрос, время приёма, сохранённый OCR и закреплённое поколение."""
    attempt: ProcessingAttempt = Field(description='Активная попытка, полученная при атомарном взятии работы в PostgreSQL.')
    input: QuestionInput = Field(description='Текстовый вопрос либо Telegram file_id изображения с подписью.')
    received_at: AwareDatetime = Field(description='Первое время приёма вопроса в UTC; повтор его не изменяет.')
    corpus_generation: NonNegativeInt = Field(description='Поколение набора доступных редакций; 0 допустим для пустого корпуса.')
    prepared_text: NonEmptyText | None = Field(default=None, description='Сохранённые подпись и OCR; None до первого успешного извлечения.')


class ImageContent(DTO):
    """Байты и MIME только для текущего вызова; не сообщение очереди."""
    data: bytes = Field(description='Проверенные байты изображения только для текущего вызова.')
    mime_type: NonEmptyText = Field(description='Проверенный формат содержимого, например image/png.')


class ImageRef(DTO):
    """Неизменяемый UUID изображения S3 и его описание; без байтов и секретных URL."""
    image_id: UUID = Field(description='Неизменяемый UUID объекта S3; не произвольный URL.')
    description: NonEmptyText = Field(description='Текстовое описание изображения для Markdown и поиска.')


class SourceLocator(DTO):
    """Положение фрагмента внутри документа; неизвестные координаты можно опустить."""
    page: PositiveId | None = Field(default=None, description='Номер страницы с 1; None или отсутствие поля означает неизвестную страницу.')
    section: NonEmptyText | None = Field(default=None, description='Заголовок или путь раздела; None или отсутствие поля — положение неизвестно.')


class SourceRef(DTO):
    """Метаданные цитируемого чанка для ответа; полный текст сюда не входит."""
    chunk_id: UUID = Field(description='Детерминированный UUID точки: UUIDv5 от revision_id и chunk_no.')
    document_id: UUID = Field(description='Стабильный ID документа, общий для всех его редакций.')
    revision_id: UUID = Field(description='ID конкретной редакции документа; совпадает с job_id индексации.')
    source_name: NonEmptyText = Field(description='Читаемое название источника для оформления ответа.')
    locator: SourceLocator | None = Field(default=None, description='Необязательный внутренний локатор; ссылка на раздел не требуется.')
    source_url: str | None = Field(default=None, description='В MVP None; ссылку на HTML-заглушку строит RAG API по document_id.')


class SearchFragment(SourceRef):
    """Найденный чанк: текст для генерации и унаследованные метаданные источника."""
    text: NonEmptyText = Field(description='Текст фрагмента для последующей обработки.')
    images: list[ImageRef] = Field(default_factory=list, description='ID и описания разрешённых изображений; байты здесь не хранятся.')

    @model_validator(mode='after')
    def check_images(self) -> Self:
        """Сверить плейсхолдеры с изображениями этого чанка."""
        _check_image_markers(self.text, [image.image_id for image in self.images])
        return self


class RagResult(DTO):
    """Готовый пользовательский ответ с источниками для сохранения и доставки."""
    answer_text: NonEmptyText = Field(description='Готовый текст ответа пользователю до Telegram-оформления.')
    sources: list[SourceRef] = Field(description='Метаданные источников ответа; пустой список допустим для служебного ответа.')
    images: list[ImageRef] = Field(default_factory=list, description='ID и описания разрешённых изображений; байты здесь не хранятся.')

    @model_validator(mode='after')
    def check_result(self) -> Self:
        """Проверить плейсхолдеры ответа и отсутствие повторных источников."""
        _check_image_markers(self.answer_text, [image.image_id for image in self.images])
        if len({source.chunk_id for source in self.sources}) != len(self.sources):
            raise ValueError('Источники ответа не должны повторяться')
        return self


class AnsweredOutcome(DTO):
    """Успешный ответ по источникам; сохраняется независимо от доставки."""
    kind: Literal['answered'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    result: RagResult = Field(description='Готовый ответ с Markdown, источниками и ID изображений.')

    @model_validator(mode='after')
    def require_sources(self) -> Self:
        """Отличить подтверждённый ответ от служебного сообщения без источников."""
        if not self.result.sources:
            raise ValueError('Ответ по документации требует источника')
        return self


class SupportOutcome(DTO):
    """Причина автоматической эскалации; тикет создаёт компонент Support."""
    kind: Literal['support'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    reason: Literal['out_of_scope', 'no_evidence'] = Field(description='Типизированная причина обращения или эскалации.')


class NoticeOutcome(DTO):
    """Служебное пояснение об отказе, неверном вводе или техническом сбое."""
    kind: Literal['notice'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    code: Literal['blocked', 'invalid_input', 'no_text', 'technical_error'] = Field(description='Стабильный код исхода для вызывающего модуля.')
    text: NonEmptyText = Field(description='Безопасное служебное сообщение пользователю, не ответ по документам.')


QuestionOutcome = Annotated[AnsweredOutcome | SupportOutcome | NoticeOutcome, Field(discriminator='kind')]


class AllowedQuestion(DTO):
    """Guardrails разрешил вопрос и вернул нормализованный текст для следующих шагов."""
    kind: Literal['allowed'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    normalized_question: NonEmptyText = Field(description='Нормализованный разрешённый вопрос; переиспользуется кешем и поиском.')


class RejectedQuestion(DTO):
    """Guardrails отклонил инъекцию или некорректный ввод; дальнейший поиск не нужен."""
    kind: Literal['blocked', 'invalid_input'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')


class OutOfScope(DTO):
    """Низкое сходство с примерами темы; основной поиск не запускается."""
    kind: Literal['out_of_scope'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')


class DetectorUnavailable(DTO):
    """Проверку защиты выполнить не удалось; это технический исход, а не разрешение."""
    kind: Literal['detector_unavailable'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    retryable: bool = Field(description='Можно ли повторить операцию по правилам владельца этапа.')


GuardrailsResult = Annotated[AllowedQuestion | RejectedQuestion | OutOfScope | DetectorUnavailable, Field(discriminator='kind')]


class GeneratedAnswer(DTO):
    """Ответ по документам с ID процитированных чанков; Core собирает SourceRef."""
    kind: Literal['answered'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    answer_text: NonEmptyText = Field(description='Готовый текст ответа пользователю до Telegram-оформления.')
    cited_chunk_ids: Annotated[list[UUID], Field(min_length=1)] = Field(description='Непустой список ID процитированных чанков из переданного контекста.')
    cited_image_ids: list[UUID] = Field(default_factory=list, description='ID использованных изображений только из разрешённого контекста.')

    @model_validator(mode='after')
    def check_image_citations(self) -> Self:
        """Сверить служебные маркеры с возвращаемыми ID изображений."""
        _check_image_markers(self.answer_text, self.cited_image_ids)
        return self


class InsufficientEvidence(DTO):
    """Генератор не нашёл достаточных оснований для ответа в переданных фрагментах."""
    kind: Literal['insufficient_evidence'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')


GenerationResult = Annotated[GeneratedAnswer | InsufficientEvidence, Field(discriminator='kind')]


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
