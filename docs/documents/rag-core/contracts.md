---
title: "Общие DTO"
sidebar_position: 3
toc_max_heading_level: 2
---

## Очередь и готовый результат

| Маршрут | DTO и JSON | Назначение |
| --- | --- | --- |
| `rag.questions` | `RequestRef`: `{"request_id":1042}` | Взять сохранённый вопрос. |
| `rag.results` | `DeliveryRef`: `{"delivery_id":2085}` | Взять самостоятельную доставку. |
| `ingestion.jobs` | `JobRef`: `{"job_id":"…"}` | Индексировать подготовленный Markdown. |

`request_id` и `delivery_id` не взаимозаменяемы. Тексты и файлы в RabbitMQ не передаются. `ConsumeDecision = Literal["ack", "retry_broker"]`: подтверждение после надёжного перехода либо повтор при неизвестном состоянии PostgreSQL.

## Общие DTO

Все классы наследуют `rag.dto.DTO` (Pydantic v2), неизвестные поля запрещены. Union валидируются через TypeAdapter с discriminator. UUID и время сериализуются строками; время содержит часовой пояс UTC.

~~~python
from typing import Self
from pydantic import model_validator
from rag.dto import _check_image_markers

from typing import Annotated, Literal
from uuid import UUID
from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText
ConsumeDecision = Literal['ack', 'retry_broker']

class RequestRef(DTO):
    request_id: PositiveId

class DeliveryRef(DTO):
    delivery_id: PositiveId

class ProcessingAttempt(DTO):
    request_id: PositiveId
    attempt_no: PositiveId
    deadline_at: AwareDatetime

class DeliveryAttempt(DTO):
    delivery_id: PositiveId
    attempt_no: PositiveId
    deadline_at: AwareDatetime

class TextQuestion(DTO):
    kind: Literal['text']
    text: NonEmptyText

class ImageQuestion(DTO):
    kind: Literal['image']
    file_id: NonEmptyText
    caption: str | None = None
QuestionInput = Annotated[TextQuestion | ImageQuestion, Field(discriminator='kind')]

class ClaimedQuestion(DTO):
    attempt: ProcessingAttempt
    input: QuestionInput
    received_at: AwareDatetime
    corpus_generation: NonNegativeInt
    prepared_text: NonEmptyText | None = None

class ImageContent(DTO):
    data: bytes
    mime_type: NonEmptyText

class ImageRef(DTO):
    image_id: UUID
    description: NonEmptyText

class SourceLocator(DTO):
    page: PositiveId | None = None
    section: NonEmptyText | None = None

class SourceRef(DTO):
    chunk_id: UUID
    document_id: UUID
    revision_id: UUID
    source_name: NonEmptyText
    locator: SourceLocator | None = None
    source_url: str | None = None

class SearchFragment(SourceRef):
    text: NonEmptyText
    images: list[ImageRef] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_images(self) -> Self:
        """Сверить плейсхолдеры с изображениями этого чанка."""
        _check_image_markers(self.text, [image.image_id for image in self.images])
        return self

class RagResult(DTO):
    answer_text: NonEmptyText
    sources: list[SourceRef]
    images: list[ImageRef] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_result(self) -> Self:
        """Проверить плейсхолдеры ответа и отсутствие повторных источников."""
        _check_image_markers(self.answer_text, [image.image_id for image in self.images])
        if len({source.chunk_id for source in self.sources}) != len(self.sources):
            raise ValueError('Источники ответа не должны повторяться')
        return self

class AnsweredOutcome(DTO):
    kind: Literal['answered']
    result: RagResult

    @model_validator(mode='after')
    def require_sources(self) -> Self:
        """Отличить подтверждённый ответ от служебного сообщения без источников."""
        if not self.result.sources:
            raise ValueError('Ответ по документации требует источника')
        return self

class SupportOutcome(DTO):
    kind: Literal['support']
    reason: Literal['out_of_scope', 'no_evidence']

class NoticeOutcome(DTO):
    kind: Literal['notice']
    code: Literal['blocked', 'invalid_input', 'no_text', 'technical_error']
    text: NonEmptyText
QuestionOutcome = Annotated[AnsweredOutcome | SupportOutcome | NoticeOutcome, Field(discriminator='kind')]
~~~

| Поля | Тип | Зачем нужны | Пример |
| --- | --- | --- | --- |
| `request_id / delivery_id` | `PositiveId` | Вопрос / отдельная доставка. | `1042 / 2085` |
| `attempt_no / deadline_at` | число / UTC-время | Право попытки на переход; срок отдельно от метрики 20 секунд. | `1 / "2026-11-14T12:02:00Z"` |
| `input.kind / text` | `"text" / str` | Самостоятельный текстовый вопрос. | `{"kind":"text","text":"Где фильтр?"}` |
| `input.file_id / caption` | `str / str или None` | Telegram-ссылка на входящее изображение и подпись. | `"AgAC…" / "Ошибка входа"` |
| `received_at` | `AwareDatetime` | Первый приём, не меняется при повторе. | `"2026-11-14T12:00:00Z"` |
| `prepared_text` | `str или None` | Сохранённый текст OCR + подпись; повтор не зависит от файла Telegram. | `"Ошибка AUTH-17"` |
| `corpus_generation` | `NonNegativeInt` | Один состав для темы, кеша и поиска. | `42` |
| `ImageContent.data / mime_type` | bytes / str | Содержимое изображения только в памяти вызова, не JSON очереди. | `b"…" / "image/png"` |
| `ImageRef.image_id / description` | `UUID / str` | ID изображения S3 и текстовое описание, без URL/байтов. | `"e1246aa5-3bba-4a74-8445-60b6271c0551" / "Кнопка фильтра"` |
| SourceRef.chunk_id | UUID | ID выбранного чанка; приходит из Qdrant. | UUIDv5 редакции и номера |
| SourceRef.document_id | UUID | Документ целиком, ID в ссылке /sources/. | UUID |
| SourceRef.revision_id | UUID | Редакция контекста вопроса. | UUID |
| SourceRef.source_name | NonEmptyText | Название источника, копируется из чанка. | Реестр мебели |
| SourceRef.locator | SourceLocator или None | Необязательный внутренний локатор; для ссылки раздел не требуется. | None |
| SourceRef.source_url | str или None | В MVP None: API строит URL HTML-заглушки из document_id. | None |
| SourceLocator.page | PositiveId или None | Номер страницы с 1, если известен. | 4 |
| SourceLocator.section | NonEmptyText или None | Заголовок раздела, если известен. | Установка |
| `SearchFragment.text / images` | текст / список | Контекст и допустимые изображения именно этого чанка. | `"Нажмите…", [ImageRef(...)]` |
| `RagResult.*` | Markdown, списки источников/изображений | Готовый ответ для кеша и доставки; ID уникальны. У answered источники непустые. | `answer_text="Откройте фильтр"` |
| `QuestionOutcome.kind` | `answered / support / notice` | Бизнес-исход без распознавания текста ответа. | `{"kind":"support","reason":"no_evidence"}` |

Целевой плейсхолдер — `![Описание](image://<uuid>)`; описание экранируется по Markdown. ID в тексте и `RagResult.images` должны совпадать. Генератор получает только изображения найденных фрагментов. RAG API заменяет служебные маркеры видимым описанием и отправляет файлы отдельно; S3-ключ разрешает Runtime, не модель.

## Исходы шагов

~~~python
from typing import Self
from pydantic import model_validator
from rag.dto import _check_image_markers

class AllowedQuestion(DTO):
    kind: Literal['allowed']
    normalized_question: NonEmptyText

class RejectedQuestion(DTO):
    kind: Literal['blocked', 'invalid_input']

class OutOfScope(DTO):
    kind: Literal['out_of_scope']

class DetectorUnavailable(DTO):
    kind: Literal['detector_unavailable']
    retryable: bool
GuardrailsResult = Annotated[AllowedQuestion | RejectedQuestion | OutOfScope | DetectorUnavailable, Field(discriminator='kind')]

class GeneratedAnswer(DTO):
    kind: Literal['answered']
    answer_text: NonEmptyText
    cited_chunk_ids: Annotated[list[UUID], Field(min_length=1)]
    cited_image_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_image_citations(self) -> Self:
        """Сверить служебные маркеры с возвращаемыми ID изображений."""
        _check_image_markers(self.answer_text, self.cited_image_ids)
        return self

class InsufficientEvidence(DTO):
    kind: Literal['insufficient_evidence']
GenerationResult = Annotated[GeneratedAnswer | InsufficientEvidence, Field(discriminator='kind')]

class StepError(Exception):
    code: str
    retryable: bool

class StorageError(Exception):
    pass
~~~

`out_of_scope`, пустой корпус/выдача и `insufficient_evidence` дают SupportOutcome; отказ/бессмыслица — NoticeOutcome. Отсутствие индекса из-за сбоя — техническая ошибка. 20 секунд не являются исходом.

Core копирует источники по cited_chunk_ids, изображения — по cited_image_ids из фактически использованного контекста; изображение связано хотя бы с одним процитированным чанком. Сверяет ID с Markdown, неизвестный ID — StepError без загрузки произвольного URL. В Exact Cache попадает только RagResult успешного ответа.

## Сигнатуры вызовов

[Core](./entrypoints.md) · [Поток](./flow.md) · [API](../telegram/module/entrypoints.md) · [DTO API](../telegram/module/contracts.md) · [PostgreSQL](../infrastructure/postgresql/index.md)

True от атомарного перехода подтверждает коммит; False — потерю права на переход. Неизвестный коммит — StorageError, не False. [Правила Pydantic](../faq/dto.md). Каркас использует эти типы; прикладная оркестрация и адаптеры пока являются заглушками.
