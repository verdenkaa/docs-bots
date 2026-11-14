---
title: "Контракты PostgreSQL · Индексация"
sidebar_position: 4
toc_max_heading_level: 2
---

Адаптер поднимает `StorageError` при недоступности PostgreSQL или неизвестном коммите. Время передаём в UTC. [Схема PostgreSQL](./index.md). [DTO Ingestion](../../ingestion/contracts.md#dto).

## <span className="operation-badge operation-badge--db">DB</span> `register_revision` {#register-revision}

~~~python
async def register_revision(revision_id: UUID, document_id: UUID | None, staging_ref: str, source_name: str, image_ids: Sequence[UUID]) -> AcceptedUpload | None: ...
~~~

**Кто вызывает.** Ingestion Gateway после записи во временный том.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `revision_id` | `UUID` | ID редакции, равный ID задания индексации. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `document_id` | `UUID \| None` | ID обновляемого документа. None означает, что нужно выделить новый ID. | `None` |
| `staging_ref` | `str` | Ключ готового Markdown и манифеста, не оригинала. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |
| source_name | str, непустой | Название исходного документа из UploadSource.filename для чанков и HTML-заглушки. | Реестр мебели.docx |
| image_ids | Sequence[UUID] | ID уже сохранённых Gateway изображений; [] допустимо. | [] |

### Что отдаёт

**Формат.** Объект Pydantic `AcceptedUpload` или `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `kind` | `Literal["accepted"]` | Задание и outbox зафиксированы. | `"accepted"` |
| `document_id` | `UUID` | ID нового или обновляемого документа. | `UUID("43f95f66-7343-4cbc-beb5-19fd36a8ca77")` |
| `job_id` | `UUID` | ID задания индексации, равный revision_id. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `return` | `None` | Передан неизвестный ID документа. | `None` |

### Правила и ошибки

Сохраняет source_name и image_ids в строке редакции, чтобы метаданные не исчезли после удаления временного Markdown. Повтор той же операции с тем же revision_id возвращает уже зарегистрированное задание; отличающиеся метаданные под тем же ID — конфликт. `revision_no` выделяется под блокировкой по документу. Регистрацию также согласуем по `revision_id` с [проверкой оставшихся загрузок](./cleanup.md#is-staging-referenced).

## <span className="operation-badge operation-badge--db">DB</span> `claim_ingestion_job` {#claim-ingestion-job}

~~~python
async def claim_ingestion_job(job_id: UUID) -> ClaimedIngestionJob | None: ...
~~~

**Кто вызывает.** Ingestion Worker.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `job_id` | `UUID` | ID из JobRef, равный ID редакции. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |

### Что отдаёт

**Формат.** Объект Pydantic `ClaimedIngestionJob` или `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `attempt` | `IngestionAttempt` | Активная попытка с номером и сроком. | `{"job_id": UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139"), "attempt_no": 1, "deadline_at": ...}` |
| `document_id` | `UUID` | ID документа для payload точек. | `UUID("43f95f66-7343-4cbc-beb5-19fd36a8ca77")` |
| `staging_ref` | `str` | Ключ загрузки для open_staged_upload. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |
| `expected_chunk_count` | `int или None` | Сохранённое число чанков; None до первой записи. | 100 |
| `expected_topic_count` | `int или None` | Сохранённое число примеров; None до первой записи. | 12 |
| `return` | `None` | Задание отсутствует, занято, завершено или ещё не готово к запуску. | `None` |

### Правила и ошибки

Внутри одного документа новая редакция ждёт публикации или окончательного сбоя предыдущей. Взятие доступно для готовой `queued` или для `indexing` с истёкшим `resume_at`, если `qdrant_write_pending=false`. Взятие атомарно устанавливает `indexing`, увеличивает `attempt_no` и записывает новый срок попытки в `resume_at`. Для `queued` это поле означает время, с которого можно взять задание. Пустое значение в `queued` означает готовность по времени. Runtime запускает нового потребителя только после выхода прежнего процесса и его потомков.

Если последняя разрешённая попытка истекла, следующий claim выдаёт попытку только для завершения с ошибкой. Worker сравнивает `attempt_no` с `max_attempts` из общей конфигурации Runtime. При превышении сразу вызывает `fail_ingestion_job`.

## <span className="operation-badge operation-badge--db">DB</span> `begin_revision_write` {#begin-revision-write}

~~~python
async def begin_revision_write(attempt: IngestionAttempt, expected_chunk_count: int, expected_topic_count: int) -> bool: ...
~~~

**Кто вызывает.** Ingestion Worker непосредственно перед первым запросом записи точек в Qdrant.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `attempt` | `IngestionAttempt` | Право текущей попытки начать запись. | `{"job_id": revision_id, "attempt_no": 1, "deadline_at": ...}` |
| `expected_chunk_count` | `int` | Положительное число чанков полной редакции. | `100` |
| `expected_topic_count` | `int` | Положительное число примеров вопросов этой редакции. | `12` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Числа чанков/примеров и `qdrant_write_pending=true` сохранены, запись разрешена. | `True` |
| `return` | `bool` | Попытка утратила право записи или признак уже установлен. | `False` |

### Правила и ошибки

Сверяет состояние, номер и срок попытки. Сохраняет ожидаемые числа чанков и примеров при первой записи. При расхождении любого набора повтор отклоняется; один qdrant_write_pending охватывает обе записи. Если на повторе расходится число чанков, поднимает StepError("chunk_count_mismatch", False), для числа примеров — StepError("topic_count_mismatch", False), до установки признака. Подготовленный снимок перед этим проверен и не заменяется заново сгенерированным содержимым. При `False` или неизвестном коммите Worker не отправляет запись в Qdrant.

## <span className="operation-badge operation-badge--db">DB</span> `publish_revision` {#publish-revision}

~~~python
async def publish_revision(attempt: IngestionAttempt) -> int | None: ...
~~~

**Кто вызывает.** Ingestion Worker после подтверждения готовности всех точек.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `attempt` | `IngestionAttempt` | Активная попытка из claim_ingestion_job. | `{"job_id": UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139"), "attempt_no": 1, "deadline_at": ...}` |

### Что отдаёт

**Формат.** Одно целое число или `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `int` | Новое поколение корпуса после коммита. | `42` |
| `return` | `None` | Попытка больше не имеет права на запись. | `None` |

### Правила и ошибки

Публикация требует qdrant_write_pending и положительные expected_chunk_count/expected_topic_count. До перехода проверены полные наборы чанков и примеров. После подтверждения записи и проверки полного набора точек Worker вызывает этот переход. Транзакция снимает признак, переводит редакцию в `published`, выводит предыдущую в `retired`, меняет интервалы видимости и увеличивает поколение. Оба ожидаемых числа остаются в строке редакции.

## <span className="operation-badge operation-badge--db">DB</span> `schedule_ingestion_retry` {#schedule-ingestion-retry}

~~~python
async def schedule_ingestion_retry(attempt: IngestionAttempt, retry_at: datetime) -> bool: ...
~~~

**Кто вызывает.** Ingestion Worker.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `attempt` | `IngestionAttempt` | Активная попытка из claim_ingestion_job. | `{"job_id": UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139"), "attempt_no": 1, "deadline_at": ...}` |
| `retry_at` | `datetime` | Время, с которого можно повторить работу. Дата и время в UTC. | `datetime(2026, 11, 10, 10, 0, tzinfo=timezone.utc)` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Задание возвращено в queued, время и outbox ingestion.jobs сохранены. | `True` |
| `return` | `bool` | Переход не выполнен, например попытка потеряла право на запись. | `False` |

### Правила и ошибки

Переход разрешён только при `qdrant_write_pending=false`.

## <span className="operation-badge operation-badge--db">DB</span> `fail_ingestion_job` {#fail-ingestion-job}

~~~python
async def fail_ingestion_job(attempt: IngestionAttempt) -> bool: ...
~~~

**Кто вызывает.** Ingestion Worker.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `attempt` | `IngestionAttempt` | Активная попытка из claim_ingestion_job. | `{"job_id": UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139"), "attempt_no": 1, "deadline_at": ...}` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Состояние failed сохранено. Неопубликованные точки доступны для уборки. | `True` |
| `return` | `bool` | Переход не выполнен, например попытка потеряла право на запись. | `False` |

### Правила и ошибки

Переход разрешён только при `qdrant_write_pending=false`. Причину ошибки пишем в диагностику с `job_id` и `attempt_no`. Если последняя разрешённая попытка истекла, сбой фиксирует новая действующая попытка, полученная через `claim_ingestion_job`.

## <span className="operation-badge operation-badge--db">DB</span> `list_pending_revision_writes` {#list-pending-revision-writes}

~~~python
async def list_pending_revision_writes(limit: int) -> list[PendingRevisionWrite]: ...
~~~

**Кто вызывает.** Операторская команда Runtime после остановки всех писателей индекса.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `limit` | `int` | Положительный предел пачки. | `20` |

### Что отдаёт

**Формат.** Список объектов Pydantic `PendingRevisionWrite`. Пустой список допустим.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `[].revision_id` | `UUID` | ID неопубликованной редакции для сверки в Qdrant. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `[].attempt_no` | `int` | Номер попытки для проверки перед снятием признака. | `1` |

### Правила и ошибки

Выбирает только `indexing` с истёкшим сроком и `qdrant_write_pending=true`. Если срок ещё действует, операторская команда ждёт его истечения перед завершением сверки. Читает кандидатов под блокировками строк и повторно проверяет условия после получения каждой блокировки, чтобы дождаться возможного коммита публикации. При таймауте блокировки сверку этой записи откладываем. Успешно опубликованные редакции в выборку не попадают.

## <span className="operation-badge operation-badge--db">DB</span> `clear_revision_write_pending` {#clear-revision-write-pending}

~~~python
async def clear_revision_write_pending(revision_id: UUID, attempt_no: int) -> bool: ...
~~~

**Кто вызывает.** Операторская команда Runtime после подтверждённой очистки по [протоколу сверки](../qdrant/index.md#сверка-незавершённых-записей).

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `revision_id` | `UUID` | Редакция из выборки на сверку. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `attempt_no` | `int` | Номер той же истёкшей попытки. | `1` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Признак снят, редакция доступна для восстановления через outbox. | `True` |
| `return` | `bool` | Условия перехода больше не выполняются. | `False` |

### Правила и ошибки

Под блокировкой строки сверяет `indexing`, истёкший срок, номер попытки и установленный признак. Меняет только признак. Срок, число попыток и оба ожидаемых числа сохраняются; PreparedRevision также сохраняется для повтора. При неизвестном коммите сначала перечитываем состояние.

## <span className="operation-badge operation-badge--db">DB</span> `list_required_revision_manifests` {#list-required-revision-manifests}

~~~python
async def list_required_revision_manifests() -> list[RevisionManifest]: ...
~~~

**Кто вызывает.** Runtime при восстановлении индекса, пока обычная обработка и изменения индекса приостановлены.

### Что принимает

Параметров нет.

### Что отдаёт

**Формат.** Список объектов Pydantic `RevisionManifest`. Пустой список означает, что ни одна редакция не нужна поиску.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `[].revision_id` | `UUID` | Уникальный ID редакции для сверки точек. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `[].expected_chunk_count` | `int или None` | Полный ожидаемый набор чанков. None означает неизвестную полноту старой редакции. | 100 |
| `[].expected_topic_count` | `int или None` | Полный ожидаемый набор примеров; None требует восстановления из доверенного манифеста. | 12 |

### Правила и ошибки

В одном согласованном чтении выбирает объединение редакций, видимых в текущем поколении и во всех сохранённых `pinned_generation`. Каждую редакцию возвращает один раз.

## Согласование переходов

Публикация, фиксация ошибки и повтор сверяют состояние `indexing`, номер и срок попытки. Все переходы редакции держат блокировку её строки до коммита и сравнивают срок с текущим временем PostgreSQL после получения нужных блокировок. После публикации или ошибки ставим следующую готовую редакцию этого документа в outbox в той же транзакции. Периодический [`recover_pending_work`](./outbox.md#recover-pending-work) подбирает готовые задания без ожидающей записи outbox и истёкшие попытки без признака незавершённой записи. [Как Worker подтверждает доставку](../../ingestion/worker.md#повторы-и-подтверждение-очереди)

## DTO

~~~python
from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText
from dataclasses import dataclass

class PendingRevisionWrite(DTO):
    revision_id: UUID
    attempt_no: PositiveId


class RevisionManifest(DTO):
    revision_id: UUID
    expected_chunk_count: PositiveId | None
    expected_topic_count: PositiveId | None
~~~
