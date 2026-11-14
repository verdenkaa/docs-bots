---
title: "Контракты Qdrant"
sidebar_position: 2
toc_max_heading_level: 2
---

[Правила DTO и Pydantic](../../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.

При сбое Qdrant или несовместимой схеме адаптер поднимает `StepError(code, retryable)`. [Схема точки и запуск](./index.md). Эмбеддинги вычисляет [Model Gateway](../../model-gateway/entrypoints.md#embed-texts); этот адаптер получает готовые векторы.

## <span className="operation-badge operation-badge--vec">VEC</span> `find_chunks` {#find-chunks}

~~~python
async def find_chunks(query: str, query_vector: Sequence[float], revision_ids: frozenset[UUID], candidate_limit: int, *, lexical_priority: bool = False) -> list[QdrantHit]: ...
~~~

**Кто вызывает.** Searcher после получения эмбеддинга вопроса.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Нормализованный вопрос из Guardrails. | `"Где инструкция по установке?"` |
| `query_vector` | `Sequence[float]` | Вектор для плотной ветки поиска. | `[0.15, -0.23, 0.41, ...]` |
| `revision_ids` | `frozenset[UUID]` | Разрешённые редакции для обеих веток. | `frozenset({UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")})` |
| `candidate_limit` | `int` | Положительный предел числа кандидатов до отбора. | `10` |
| `lexical_priority` | `bool` | Выбранный Searcher профиль: больший вес BM25 для вопроса со ссылкой. | `True` |

### Что отдаёт

**Формат.** Список объектов Pydantic `QdrantHit` в порядке поискового профиля. `[]` при пустом наборе редакций или отсутствии совпадений.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `[].fragment` | `SearchFragment` | Текст и метаданные точки для Searcher. | `{"chunk_id": UUID("545f64ff-7692-579a-842a-31f87e6522df"), ...}` |
| `[].score` | `float` | Балл выбранного профиля для отбора в Searcher. | `0.72` |

### Правила и ошибки

Runtime получает выбранный Searcher признак lexical_priority и исполняет профиль weighted RRF; при True относительный вес BM25 больше dense. Численные веса и пределы веток заданы конфигурацией. Обе ветки фильтруются по одному revision_ids до top-k. Балл используется внутри Searcher. [Поля фрагмента](../../retrieval/entrypoints.md#search).

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

Собирает ID чанков и примеров по точному фильтру `revision_id` в обоих наборах и удаляет по явному списку ID. Для каждого пакета ждём статус `completed` с `wait=true`, затем проверяем пустой набор через verify_revision_points(revision_id,0) и verify_topic_points(revision_id,0) в обоих наборах. При неизвестном исходе поднимаем `StepError`. Для окончательно выведенной из обращения редакции Worker может повторять удаление. Для неопубликованной редакции, которую ещё можно записать заново, действуют [правила операторской сверки](./index.md#сверка-незавершённых-записей). [Удаление точек Qdrant](https://api.qdrant.tech/api-reference/points/delete-points)

## DTO

~~~python
from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText
from dataclasses import dataclass

class ChunkPoint(SearchFragment):
    chunk_no: NonNegativeInt
    dense: list[float]


class QdrantHit(DTO):
    fragment: SearchFragment
    score: float
~~~

[`SearchFragment`](../../rag-core/contracts.md#общие-dto) описан в общих DTO.

## <span className="operation-badge operation-badge--vec">VEC</span> `find_topic_matches` {#find-topic-matches}

~~~python
async def find_topic_matches(query_vector: Sequence[float], revision_ids: frozenset[UUID], limit: int = 1) -> list[TopicHit]: ...
~~~

**Кто вызывает.** Guardrails после embed_texts(query,purpose="query").

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| query_vector | Sequence[float] | Вектор нормализованного вопроса. | [0.1,...] |
| revision_ids | frozenset[UUID] | Тот же набор, который Core передаст Searcher. | frozenset(&#123;UUID(...)&#125;) |
| limit | int > 0 | Максимум ближайших примеров. | 1 |

### Что отдаёт

**Формат.** Список TopicHit.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| [].revision_id | UUID | Редакция примера. | UUID(...) |
| [].score | float в [-1,1] | Косинусное сходство, не вероятность. | 0.61 |

### Правила и ошибки

Читает отдельный набор примеров, отфильтрованный по revision_ids до top-k. Пустое множество → []; отсутствующий/несовместимый индекс → StepError, не успешный пустой ответ. Профиль совпадает с query-embedding примеров. Низкое сходство трактует Guardrails, не Runtime. Не применяет серверный порог score к примерам: для непустого исправного набора ближайший результат нужен даже с низким сходством; [] при непустых revision_ids означает техническую неполноту.

~~~python
class TopicHit(DTO):
    revision_id: UUID
    score: float

class TopicPoint(DTO):
    point_id: UUID
    revision_id: UUID
    question_no: NonNegativeInt
    text: NonEmptyText
    dense: list[float]
~~~

TopicPoint.point_id = UUIDv5(revision_id, "question:" + str(question_no)), нумерация с 0. Вектор примера кодируется с purpose=query. Описание text нужно для диагностики индекса, не как источник ответа.

## <span className="operation-badge operation-badge--db">DB</span> `write_topic_points` {#write-topic-points}

~~~python
async def write_topic_points(revision_id: UUID, points: Sequence[TopicPoint]) -> None: ...
~~~

**Кто вызывает.** Worker после записи чанков и до publish_revision.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| revision_id | UUID | Подготовленная редакция. | UUID(...) |
| points | Sequence[TopicPoint] | Полный непустой набор с детерминированными ID. | [TopicPoint(...)] |

### Что отдаёт

**Формат.** None.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | None | Все пакеты применены и набор ID сверён с expected_topic_count. | None |

### Правила и ошибки

Использует общий qdrant_write_pending, установленный до обеих записей. wait=true, ограниченные пакеты и тот же протокол неизвестного исхода, что у write_revision_points. При таймауте следующая запись/публикация запрещены до сверки.

## <span className="operation-badge operation-badge--vec">VEC</span> `verify_topic_points` {#verify-topic-points}

~~~python
async def verify_topic_points(revision_id: UUID, expected_topic_count: int) -> bool: ...
~~~

**Кто вызывает.** Worker/восстановление индекса.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| revision_id | UUID | Проверяемая редакция. | UUID(...) |
| expected_topic_count | int >= 0 | Ожидание из PostgreSQL, 0 для очистки. | 12 |

### Что отдаёт

**Формат.** bool.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | bool | Полный набор ID совпадает с ожидаемым, а не просто присутствует редакция. | True |

### Правила и ошибки

Сравнивает точный набор UUIDv5 и соответствие question_no каждому ID, без пропусков/повторов/лишних точек. Количество точек восстановленного снимка не используется как ожидаемое.
