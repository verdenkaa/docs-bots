---
title: "Контракты PostgreSQL · Индексация"
sidebar_position: 4
toc_max_heading_level: 2
---

Адаптер поднимает `StorageError` при недоступности PostgreSQL или неизвестном коммите. Время передаём в UTC. [Схема PostgreSQL](./index.md). [DTO Ingestion](../../ingestion/contracts.md#dto).

## <span className="operation-badge operation-badge--db">DB</span> `register_revision` {#register-revision}

~~~python
async def register_revision(revision_id: UUID, document_id: UUID | None, staging_ref: str, source: UploadSource, image_ids: Sequence[UUID]) -> AcceptedUpload | None:
    ...
~~~

**Кто вызывает.** Ingestion Gateway после записи во временный том.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `revision_id` | `UUID` | ID редакции, равный ID задания индексации. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `document_id` | `UUID \| None` | ID обновляемого документа. None означает, что нужно выделить новый ID. | `None` |
| `staging_ref` | `str` | Ключ готовой партии, Markdown и манифеста, не оригинала. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |
| source | UploadSource | Точное имя и обязательная метка доступа из сохранённой DocumentBatch. | filename="Реестр мебели.docx", access_level="employees" |
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

Сохраняет source.filename как source_name, source.access_level и image_ids в строке редакции, чтобы метаданные не исчезли после удаления временного Markdown. Повтор той же операции с тем же revision_id возвращает уже зарегистрированное задание; отличающиеся метаданные под тем же ID — конфликт. `revision_no` как порядок загрузки выделяется под блокировкой по документу; это не document_version. При document_id=None под блокировкой точного source.filename находит/создаёт строку documents. Явный ID должен совпадать с владельцем имени; неизвестный ID даёт None, конфликт имени — StepError(document_name_conflict, False). Регистрация требует уже сохранённой DocumentBatch. Номер публикуемой версии назначает позднее Worker через reserve_document_version. Регистрацию также согласуем по `revision_id` с [проверкой оставшихся загрузок](./cleanup.md#is-staging-referenced).

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
| document_version / base_version | int или None | Прежняя резервация версии и опубликованная база; на повторе не меняются. | 1 / 0 |
| `expected_chunk_count` | `int или None` | Сохранённое число чанков; None до первой записи. | 100 |
| `expected_topic_count` | `int или None` | Сохранённое число примеров; None до первой записи. | 12 |
| `return` | `None` | Задание отсутствует, занято, завершено или ещё не готово к запуску. | `None` |

### Правила и ошибки

Внутри одного документа новая редакция ждёт публикации или окончательного сбоя предыдущей. Взятие доступно для готовой `queued` или для `indexing` с истёкшим `resume_at`, если `qdrant_write_pending=false`. Взятие атомарно устанавливает `indexing`, увеличивает `attempt_no` и записывает новый срок попытки в `resume_at`. Для `queued` это поле означает время, с которого можно взять задание. Пустое значение в `queued` означает готовность по времени. Runtime запускает нового потребителя только после выхода прежнего процесса и его потомков.

Если последняя разрешённая попытка истекла, следующий claim выдаёт попытку только для завершения с ошибкой. Worker сравнивает `attempt_no` с `max_attempts` из общей конфигурации Runtime. При превышении сразу вызывает `fail_ingestion_job`.

Другая действующая попытка `indexing` с будущим `resume_at` блокирует взятие
задания, в том числе во время записи с `qdrant_write_pending=true`.
При завершении обработчика с необходимостью сверки приватный переход Runtime
`_release_pending_attempt` сверяет владение попыткой и установленный pending под
блокировкой, затем заменяет `resume_at` текущим временем PostgreSQL. Это завершает
право попытки на публикацию и освобождает обработку других документов. Сам pending,
состояние `indexing`, манифест и staging не меняются; outbox не создаётся.
Повтор этой редакции, её очистка и обработка следующих редакций того же документа
остаются заблокированы до операторской сверки. После аварийного обрыва без этого
перехода новая работа ждёт истечения исходного срока.

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

Сверяет состояние, номер и срок попытки, а также наличие document_version, закреплённой reserve_document_version. Сохраняет ожидаемые числа чанков и примеров при первой записи. При расхождении любого набора повтор отклоняется; один qdrant_write_pending охватывает обе записи. Если на повторе расходится число чанков, поднимает StepError("chunk_count_mismatch", False), для числа примеров — StepError("topic_count_mismatch", False), до установки признака. Подготовленный снимок перед этим проверен и не заменяется заново сгенерированным содержимым. При `False` или неизвестном коммите Worker не отправляет запись в Qdrant.

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

Публикация требует qdrant_write_pending и положительные expected_chunk_count/expected_topic_count. До перехода проверены полные наборы чанков и примеров. После подтверждения записи и проверки полного набора точек Worker вызывает этот переход. Транзакция сверяет documents.current_version с сохранённым base_version и наличие резервации; переключает current_revision_id/current_version на revision_id/document_version, снимает признак, переводит редакцию в `published`, выводит предыдущую в `retired`, меняет интервалы видимости и увеличивает поколение. Конфликт базы после начала записи оставляет pending для сверки и не публикует редакцию. Это операция публикации полного документа, не произвольная установка номера версии. Оба ожидаемых числа остаются в строке редакции.

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

## <span className="operation-badge operation-badge--db">DB</span> `read_document_version` {#read-document-version}

Кто вызывает: Worker после load_document_batch, если версия ещё не закреплена.

~~~python
async def read_document_version(source_name: str) -> DocumentVersion | None:
    ...
~~~

Вход: непустое точное регистрозависимое filename. Результат DocumentVersion содержит document_id и опубликованную document_version с 0. None означает отсутствие опубликованной версии: документа может ещё не быть либо его первая загрузка только зарегистрирована. Значение document_version=0 является найденной версией. StorageError означает техническую ошибку и не преобразуется в None. Чтение не резервирует следующую версию; Worker сверяет document_id с заданием.

## <span className="operation-badge operation-badge--db">DB</span> `reserve_document_version` {#reserve-document-version}

Кто вызывает: Worker до embedding и сохранения PreparedRevision.

~~~python
async def reserve_document_version(attempt: IngestionAttempt, expected_version: int | None) -> ReservedRevision | None:
    ...
~~~

Вход: действующая попытка и результат чтения опубликованной версии; None только для отсутствия публикации. expected_version, если задана, неотрицательна. Под блокировкой документа и редакции сверяет состояние indexing, номер/срок попытки, порядок загрузок и текущую опубликованную версию. Короткая транзакция сохраняет base_version и document_version: 0 при None, иначе expected_version + 1. Внешние вызовы выполняются после освобождения блокировок.

Результат ReservedRevision содержит revision_id, document_id, document_version и base_version; None означает утраченное право попытки. При уже существующей резервации той же редакции и совпадающей базе возвращает прежний номер, не увеличивая его. На повторе Worker передаёт сохранённый base_version. Несовпадение базы до первой резервации даёт StepError(document_version_conflict, True); несовпадение с прежней резервацией — StepError(document_version_conflict, False). Неизвестный коммит — StorageError: до проверки резервации готовить/писать другую редакцию нельзя.

Неудачная неопубликованная редакция может освободить номер для следующего задания; её revision_id остаётся другим. Опубликованные номера уникальны в истории документа. Порядок принятия загрузок определяется revision_no, а не document_version.

## DTO версий

~~~python
class DocumentVersion(DTO):
    document_id: UUID
    document_version: NonNegativeInt

class ReservedRevision(DTO):
    revision_id: UUID
    document_id: UUID
    document_version: NonNegativeInt
    base_version: NonNegativeInt | None

    @model_validator(mode='after')
    def check_version(self) -> Self:
        if self.document_version != (0 if self.base_version is None else self.base_version + 1):
            raise ValueError('Первая версия равна 0, следующая равна base_version + 1')
        return self
~~~
