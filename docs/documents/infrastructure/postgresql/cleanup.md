---
title: "Контракты PostgreSQL · Очистка"
sidebar_position: 5
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

Адаптер поднимает `StorageError` при недоступности PostgreSQL или неизвестном коммите. Время передаём в UTC. [Схема PostgreSQL](./index.md).

## <span className="operation-badge operation-badge--db">DB</span> `list_finished_staging` {#list-finished-staging}

~~~python
async def list_finished_staging(limit: int) -> list[StagingCleanup]: ...
~~~

**Кто вызывает.** Ingestion Worker.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `limit` | `int` | Положительный предел размера пачки. | `20` |

### Что отдаёт

**Формат.** Список словарей Python `StagingCleanup`. `[]` означает, что кандидатов нет.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `[].revision_id` | `UUID` | Редакция завершённого задания. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `[].staging_ref` | `str` | Ключ загрузки, которую ещё нужно убрать. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |

### Правила и ошибки

Берём завершённые задания с заполненным `staging_ref`, включая `published`, `failed`, `retired`, `deleting` и `cleaned`. Очистка точек может закончиться раньше очистки файла.

## <span className="operation-badge operation-badge--db">DB</span> `clear_staging_ref` {#clear-staging-ref}

~~~python
async def clear_staging_ref(revision_id: UUID, staging_ref: str) -> bool: ...
~~~

**Кто вызывает.** Ingestion Worker после удаления загрузки.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `revision_id` | `UUID` | ID редакции, равный ID задания индексации. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `staging_ref` | `str` | Внутренний ключ файла и манифеста во временном томе. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Ссылка очищена. | `True` |
| `return` | `bool` | Ссылка изменилась или строка больше не подходит для очистки. | `False` |



## <span className="operation-badge operation-badge--db">DB</span> `is_staging_referenced` {#is-staging-referenced}

~~~python
async def is_staging_referenced(staging_ref: str) -> bool: ...
~~~

**Кто вызывает.** Ingestion Worker при старте, до приёма загрузок.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `staging_ref` | `str` | Внутренний ключ файла и манифеста во временном томе. | `"uploads/bb32ecbf-082b-4497-a41f-4b61aa56a139"` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Ссылка есть в PostgreSQL. | `True` |
| `return` | `bool` | На загрузку нет ссылки. | `False` |

### Правила и ошибки

Для проверки используем тот же ключ блокировки по `revision_id`, что при регистрации. Так проверка дождётся исхода предыдущего коммита. Gateway ещё не принимает новые загрузки во время этой проверки.

## <span className="operation-badge operation-badge--db">DB</span> `claim_revision_cleanup` {#claim-revision-cleanup}

~~~python
async def claim_revision_cleanup(limit: int) -> list[UUID]: ...
~~~

**Кто вызывает.** Ingestion Worker.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `limit` | `int` | Положительный предел размера пачки. | `20` |

### Что отдаёт

**Формат.** Список UUID, включая ранее взятые и незавершённые удаления.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `list[UUID]` | Редакции, разрешённые к удалению. Пустой список допустим. | `[UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")]` |

### Правила и ошибки

Под блокировкой `corpus_state` берём `retired` только когда её интервал больше не нужен закреплённым запросам. `failed` можно взять сразу. Для обеих веток требуется `qdrant_write_pending=false`. Взятие переводит редакцию в `deleting`. Неопубликованные редакции с неизвестным исходом записи обрабатывает отдельная [операторская сверка](../qdrant/index.md#сверка-незавершённых-записей).

## <span className="operation-badge operation-badge--db">DB</span> `finish_revision_cleanup` {#finish-revision-cleanup}

~~~python
async def finish_revision_cleanup(revision_id: UUID) -> bool: ...
~~~

**Кто вызывает.** Ingestion Worker после подтверждённого удаления точек.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `revision_id` | `UUID` | ID редакции, равный ID задания индексации. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Состояние cleaned сохранено. | `True` |
| `return` | `bool` | Редакция уже вышла из очистки или не была взята. | `False` |

### Правила и ошибки

Метаданные редакции и границы видимости остаются в PostgreSQL. Очистка точек и временной загрузки независимы.

## DTO

~~~python
class StagingCleanup(TypedDict):
    revision_id: UUID
    staging_ref: str
~~~
