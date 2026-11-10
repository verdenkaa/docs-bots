---
title: "Контракты Qdrant и эмбеддингов"
sidebar_position: 2
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

При сбое модели, Qdrant или несовместимой схеме адаптер поднимает `StepError(code, retryable)`. [Схема точки и запуск](./index.md).

## <span className="operation-badge operation-badge--async">ASYNC</span> `embed_texts` {#embed-texts}

~~~python
async def embed_texts(texts: Sequence[str]) -> list[list[float]]: ...
~~~

**Кто вызывает.** Searcher для вопроса и Ingestion Worker для текстов чанков.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `texts` | `Sequence[str]` | Тексты для кодирования одной версией модели. | `["Где инструкция по установке?"]` |

### Что отдаёт

**Формат.** Список плотных векторов. Один вектор на каждый текст, в том же порядке.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `list[list[float]]` | Координаты эмбеддингов. Длина вектора соответствует выбранной модели. | `[[0.15, -0.23, 0.41, ...]]` |

### Правила и ошибки

Searcher передаёт первый вектор в `find_chunks`. Worker сопоставляет векторы и чанки по позиции.

## <span className="operation-badge operation-badge--vec">VEC</span> `find_chunks` {#find-chunks}

~~~python
async def find_chunks(query: str, query_vector: Sequence[float], revision_ids: frozenset[UUID], candidate_limit: int) -> list[QdrantHit]: ...
~~~

**Кто вызывает.** Searcher после получения эмбеддинга вопроса.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Нормализованный вопрос из Guardrails. | `"Где инструкция по установке?"` |
| `query_vector` | `Sequence[float]` | Вектор для плотной ветки поиска. | `[0.15, -0.23, 0.41, ...]` |
| `revision_ids` | `frozenset[UUID]` | Разрешённые редакции для обеих веток. | `frozenset({UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")})` |
| `candidate_limit` | `int` | Положительный предел числа кандидатов до отбора. | `10` |

### Что отдаёт

**Формат.** Список словарей Python `QdrantHit` в порядке поискового профиля. `[]` при пустом наборе редакций или отсутствии совпадений.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `[].fragment` | `SearchFragment` | Текст и метаданные точки для Searcher. | `{"chunk_id": UUID("545f64ff-7692-579a-842a-31f87e6522df"), ...}` |
| `[].score` | `float` | Балл выбранного профиля для отбора в Searcher. | `0.72` |

### Правила и ошибки

Режим поиска и пределы веток задаёт Runtime. Балл используется внутри Searcher. [Поля фрагмента](../../retrieval/entrypoints.md#search).

## <span className="operation-badge operation-badge--db">DB</span> `write_revision_points` {#write-revision-points}

~~~python
async def write_revision_points(revision_id: UUID, points: Sequence[ChunkPoint]) -> None: ...
~~~

**Кто вызывает.** Ingestion Worker после подготовки текстов и векторов.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `revision_id` | `UUID` | ID редакции, равный ID задания индексации. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `points` | `Sequence[ChunkPoint]` | Полный непустой набор точек одной редакции. Размер равен сохранённому `expected_chunk_count`. | `[{"chunk_id": UUID("545f64ff-7692-579a-842a-31f87e6522df"), "chunk_no": 3, "dense": [...], ...}, ...]` |

### Что отдаёт

**Формат.** Одно значение Python, `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `None` | Запись подтверждена, все ожидаемые ID доступны в этой редакции. | `None` |

### Правила и ошибки

Worker вызывает функцию после успешного `begin_revision_write`. Адаптер пишет пакеты последовательно, с `wait=true` и ограничением времени. Принимает только подтверждение `completed` для каждого пакета. Таймаут или отсутствие подтверждения прерывает цепочку и оставляет признак незавершённой записи в PostgreSQL. Без подтверждения не отправляем следующий пакет и не повторяем запись внутри адаптера. После всех подтверждений адаптер проверяет полный набор через `verify_revision_points(revision_id, len(points))`. При несовпадении поднимает `StepError("revision_points_mismatch", retryable=True)`. Повтор в этом случае тоже требует операторской сверки. [Запись точек Qdrant](https://api.qdrant.tech/api-reference/points/upsert-points)

## <span className="operation-badge operation-badge--vec">VEC</span> `verify_revision_points` {#verify-revision-points}

~~~python
async def verify_revision_points(revision_id: UUID, expected_chunk_count: int) -> bool: ...
~~~

**Кто вызывает.** Адаптер Qdrant после подтверждённой записи всех пакетов. Runtime при восстановлении индекса и сверке незавершённой записи.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `revision_id` | `UUID` | Редакция для точного фильтра payload. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `expected_chunk_count` | `int` | Число чанков из манифеста. При проверке удаления передаём `0`. | `100` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Набор точек совпадает с ожидаемым. | `True` |
| `return` | `bool` | Есть пропущенный или лишний ID либо неверный номер чанка. | `False` |

### Правила и ошибки

Читаем все страницы `scroll` по точному `revision_id` без передачи векторов. Сверяем ID с множеством `uuid5(revision_id, str(i))` для номеров от 0 до `expected_chunk_count - 1`, а `chunk_no` с номером, из которого получен ID. Для `0` ожидаем пустой набор. Сбой чтения поднимает `StepError`. Проверка набора не обнаруживает порчу содержимого под тем же ID. [Чтение точек по страницам](https://api.qdrant.tech/api-reference/points/scroll-points)

## <span className="operation-badge operation-badge--db">DB</span> `delete_revision_points` {#delete-revision-points}

~~~python
async def delete_revision_points(revision_id: UUID) -> None: ...
~~~

**Кто вызывает.** Ingestion Worker после разрешения PostgreSQL или операторская команда Runtime после остановки писателей и завершения восстановления журнала Qdrant.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `revision_id` | `UUID` | ID редакции, равный ID задания индексации. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |

### Что отдаёт

**Формат.** Одно значение Python, `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `None` | Удаление подтверждено или точек редакции уже нет. | `None` |

### Правила и ошибки

Собирает ID точек по точному фильтру `revision_id` и удаляет по явному списку ID. Для каждого пакета ждём статус `completed` с `wait=true`, затем проверяем пустой набор через `verify_revision_points(revision_id, 0)`. При неизвестном исходе поднимаем `StepError`. Для окончательно выведенной из обращения редакции Worker может повторять удаление. Для неопубликованной редакции, которую ещё можно записать заново, действуют [правила операторской сверки](./index.md#сверка-незавершённых-записей). [Удаление точек Qdrant](https://api.qdrant.tech/api-reference/points/delete-points)

## DTO

~~~python
class ChunkPoint(SearchFragment):
    chunk_no: int
    dense: list[float]


class QdrantHit(TypedDict):
    fragment: SearchFragment
    score: float
~~~

[`SearchFragment`](../../rag-core/contracts.md#общие-dto) описан в общих DTO.
