---
title: "Контракты временного тома"
sidebar_position: 2
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

[Правила DTO и Pydantic](../../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.

Адаптер поднимает `StorageError` при сбое доступа к тому. [DTO Ingestion](../../ingestion/contracts.md#dto) · [Формат манифеста](./index.md#запись-временного-файла).

## <span className="operation-badge operation-badge--async">ASYNC</span> `stage_upload` {#stage-upload}

~~~python
async def stage_upload(revision_id: UUID, content: AsyncIterable[bytes], source: UploadSource) -> str | UploadRejected: ...
~~~

**Кто вызывает.** Ingestion Gateway перед регистрацией редакции.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `revision_id` | `UUID` | ID редакции, равный ID задания индексации. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `content` | `AsyncIterable[bytes]` | Содержимое файла из входного потока. | `file_stream` |
| `source` | `UploadSource` | Имя файла для парсинга и адрес для ссылки на источник. | `{"filename": "Руководство по установке.pdf", "source_url": None}` |

### Что отдаёт

**Формат.** Строка с ключом загрузки или объект Pydantic `UploadRejected`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `str` | Ключ после устойчивой записи файла и манифеста. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |
| `kind` | `Literal["rejected"]` | Некорректные входные данные. | `"rejected"` |
| `code` | `Literal["invalid_upload", "too_large"]` | Пустой или некорректный поток либо превышение лимита. | `"too_large"` |



## <span className="operation-badge operation-badge--async">ASYNC</span> `open_staged_upload` {#open-staged-upload}

~~~python
async def open_staged_upload(staging_ref: str) -> StagedUpload | None: ...
~~~

**Кто вызывает.** Ingestion Worker перед парсингом.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `staging_ref` | `str` | Внутренний ключ файла и манифеста во временном томе. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |

### Что отдаёт

**Формат.** Объект Pydantic `StagedUpload` или `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `path` | `Path` | Локальный путь к файлу для парсера. | `Path(".../source")` |
| `source` | `UploadSource` | Имя файла и URL из манифеста. | `{"filename": "Руководство по установке.pdf", "source_url": None}` |
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
| `return` | `None` | Файл и манифест удалены или уже отсутствуют. | `None` |



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
