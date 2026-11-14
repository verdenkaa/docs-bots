---
title: "Контракты временного тома"
sidebar_position: 2
toc_max_heading_level: 2
---

[Правила DTO и Pydantic](../../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.

Адаптер поднимает `StorageError` при сбое доступа к тому. [DTO Ingestion](../../ingestion/contracts.md#dto) · [Формат манифеста](./index.md#запись-временного-файла).

## <span className="operation-badge operation-badge--async">ASYNC</span> `stage_upload` {#stage-upload}

~~~python
async def stage_upload(revision_id: UUID, document: ConvertedDocument, source: UploadSource) -> str: ...
~~~

**Кто вызывает.** Gateway после подтверждённых записей S3.

### Что принимает

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| revision_id | UUID | Редакция и ключ каталога. | UUID(...) |
| document | ConvertedDocument | Готовый Markdown и ImageRef без байтов. | ConvertedDocument(...) |
| source | UploadSource | Название оригинала для манифеста. | UploadSource(filename="Руководство.docx") |

### Что отдаёт

**Формат.** Одна строка — внутренний ключ загрузки.

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| return | str | Только после устойчивой записи converted.md и манифеста. | uploads/&lt;revision_id> |

### Правила и ошибки

Не принимает оригинал или произвольный путь. Проверяет Markdown/плейсхолдеры; несогласованный вход — StepError(invalid_upload, False). Сбой/неизвестный исход записи — StorageError. Повтор того же revision_id с тем же содержимым безопасен; отличающееся содержимое — постоянный конфликт. Gateway не регистрирует задание, пока запись не подтверждена.

## <span className="operation-badge operation-badge--async">ASYNC</span> `open_staged_upload` {#open-staged-upload}

~~~python
async def open_staged_upload(staging_ref: str) -> StagedUpload | None: ...
~~~

**Кто вызывает.** Ingestion Worker перед разбиением готового Markdown.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `staging_ref` | `str` | Внутренний ключ файла и манифеста во временном томе. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |

### Что отдаёт

**Формат.** Объект Pydantic `StagedUpload` или `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `path` | `Path` | Путь к готовому Markdown, не оригиналу. | `Path(".../converted.md")` |
| `source` | `UploadSource` | Название исходника из манифеста. | `{"filename": "Руководство.docx"}` |
| `images` | `list[ImageRef]` | Разрешённые ID/описания из манифеста. | `[]` |
| `return` | `None` | Загрузка отсутствует. | `None` |

### Правила и ошибки

Если загрузка утрачена, Worker завершает задание ошибкой.

## <span className="operation-badge operation-badge--async">ASYNC</span> `remove_staged_upload` {#remove-staged-upload}

~~~python
async def remove_staged_upload(staging_ref: str) -> None: ...
~~~

**Кто вызывает.** Gateway после подтверждённого отказа регистрации или Worker при очистке.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `staging_ref` | `str` | Внутренний ключ файла и манифеста во временном томе. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |

### Что отдаёт

**Формат.** Одно значение Python, `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `None` | Каталог с Markdown, манифестом и prepared.json удалён или уже отсутствует; S3 не затронут. | `None` |



## <span className="operation-badge operation-badge--async">ASYNC</span> `list_staging_candidates` {#list-staging-candidates}

~~~python
async def list_staging_candidates(older_than: datetime, limit: int) -> list[str]: ...
~~~

**Кто вызывает.** Ingestion Worker до запуска приёма загрузок.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `older_than` | `datetime` | Порог возраста загрузки в UTC. | `datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)` |
| `limit` | `int` | Положительный предел размера пачки. | `20` |

### Что отдаёт

**Формат.** Список строк, пустой список допустим.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `list[str]` | Ключи старых загрузок, включая прерванную запись файла или манифеста. | `["uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"]` |

### Правила и ошибки

Worker перед удалением проверяет [`is_staging_referenced`](../postgresql/cleanup.md#is-staging-referenced) в PostgreSQL. Ключ кандидата должен позволять определить `revision_id` даже без полного манифеста.

## Подготовленный набор {#prepared-revision}

ChunkPoint и TopicPoint — [DTO Qdrant](../qdrant/contracts.md#dto). Подготовленный набор — внутренний объект Runtime, не RabbitMQ-сообщение.

~~~python
from typing import Self
from pydantic import model_validator
from uuid import uuid5

from uuid import UUID
from pydantic import Field
from rag.dto import DTO, NonEmptyText
from rag.runtime.qdrant import ChunkPoint, TopicPoint

class PreparedRevision(DTO):
    revision_id: UUID
    preparation_profile: NonEmptyText
    embedding_profile: NonEmptyText
    points: list[ChunkPoint] = Field(min_length=1)
    topic_points: list[TopicPoint] = Field(min_length=1)

    @model_validator(mode='after')
    def check_points(self) -> Self:
        """Проверить детерминированные ID и совместимость двух полных наборов."""
        document_ids = {point.document_id for point in self.points}
        if len(document_ids) != 1:
            raise ValueError('Редакция содержит один document_id')
        for number, point in enumerate(self.points):
            if point.revision_id != self.revision_id or point.chunk_no != number or point.chunk_id != uuid5(self.revision_id, str(number)):
                raise ValueError('Нарушена идентичность или нумерация чанков')
        for number, point in enumerate(self.topic_points):
            if point.revision_id != self.revision_id or point.question_no != number or point.point_id != uuid5(self.revision_id, 'question:' + str(number)):
                raise ValueError('Нарушена идентичность или нумерация примеров')
        dimensions = {len(point.dense) for point in [*self.points, *self.topic_points]}
        if len(dimensions) != 1 or 0 in dimensions:
            raise ValueError('Векторы двух наборов имеют одну ненулевую размерность')
        return self
~~~

| Поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| revision_id | UUID | Совпадает с ID каталога, всех чанков и примеров. | UUID(...) |
| preparation_profile | NonEmptyText | Неизменяемая версия разбиения, промпта/профиля генерации примеров и правил подготовки. | ingestion-v1 |
| embedding_profile | NonEmptyText | Общий профиль векторов чанков и примеров. | embedding-v1 |
| points | list[ChunkPoint] | Полный упорядоченный набор чанков/источников/изображений/векторов. | [ChunkPoint(...)] |
| topic_points | list[TopicPoint] | Полный набор примеров/векторов с нумерацией с 0. | [TopicPoint(...)] |

Обязательные проверки: revision_id, document_id, уникальность и точные детерминированные ID, непрерывная нумерация, конечные координаты одной размерности, разрешённые image_id. Профили должны совпадать с конфигурацией Worker/индекса до begin_revision_write.

## <span className="operation-badge operation-badge--async">ASYNC</span> `load_prepared_revision` {#load-prepared-revision}

~~~python
async def load_prepared_revision(staging_ref: str) -> PreparedRevision | None: ...
~~~

**Кто вызывает.** Worker до новой подготовки.

### Что принимает

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| staging_ref | str | Ключ уже принятого Markdown. | uploads/&lt;revision_id> |

### Что отдаёт

**Формат.** PreparedRevision либо None.

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| return | PreparedRevision | Сохранённый полный набор для повторов. | PreparedRevision(...) |
| return | None | Набор ещё не фиксировался. | None |

### Правила и ошибки

Отсутствующий prepared.json даёт None. Повреждение/неполная устойчивая запись — StorageError, не None. При сохранённых expected-counts и потерянном наборе заново генерировать его нельзя: задание требует разбирательства/повторной загрузки.

## <span className="operation-badge operation-badge--async">ASYNC</span> `store_prepared_revision` {#store-prepared-revision}

~~~python
async def store_prepared_revision(staging_ref: str, prepared: PreparedRevision) -> None: ...
~~~

**Кто вызывает.** Worker после генерации/embedding, до begin_revision_write.

### Что принимает

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| staging_ref | str | Каталог принятой загрузки. | uploads/&lt;revision_id> |
| prepared | PreparedRevision | Полные валидированные наборы и профили. | PreparedRevision(...) |

### Что отдаёт

**Формат.** None после устойчивого сохранения.

| Параметр / поле | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| return | None | Полный снимок готов для следующей попытки. | None |

### Правила и ошибки

Не изменяет уже сохранённый набор. Совпадающий повтор идемпотентен; различающийся — StepError(prepared_revision_conflict, False). При неизвестном исходе перечитать снимок до любых записей Qdrant. Разрыв атомарной записи не делает частичный набор видимым.
