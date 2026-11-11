---
title: "Контракты Redis"
sidebar_position: 2
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

[Правила DTO и Pydantic](../../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.

Функции строят ключ из нормализованного вопроса. При недоступности Redis адаптер поднимает `CacheUnavailable`. Exact Cache преобразует сбой в промах при чтении и завершает запись без исключения. [Формат хранения](./index.md).

## <span className="operation-badge operation-badge--cache">CACHE</span> `read_cached_answer` {#read-cached-answer}

~~~python
async def read_cached_answer(query: str) -> CacheRead | None: ...
~~~

**Кто вызывает.** Exact Cache.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Нормализованный вопрос из Guardrails. | `"Где инструкция по установке?"` |

### Что отдаёт

**Формат.** Словарь Python `CacheRead` или `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `value` | `CachedAnswer` | Готовый RagResult и поколение записи. | `{"corpus_generation": 42, "answer_text": "См. раздел «Установка».", "sources": [...]}` |
| `compare_token` | `bytes` | Токен для условного удаления прочитанного значения. | `b"observed-value"` |
| `return` | `None` | Ключ отсутствует или значение некорректно. | `None` |



## <span className="operation-badge operation-badge--cache">CACHE</span> `delete_cached_answer_if_unchanged` {#delete-cached-answer-if-unchanged}

~~~python
async def delete_cached_answer_if_unchanged(query: str, compare_token: bytes) -> bool: ...
~~~

**Кто вызывает.** Exact Cache.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Нормализованный вопрос из Guardrails. | `"Где инструкция по установке?"` |
| `compare_token` | `bytes` | Токен из CacheRead, идентифицирующий прочитанное значение. | `b"observed-value"` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Ключ удалён, потому что прочитанное значение совпало. | `True` |
| `return` | `bool` | Ключ отсутствует или был изменён. | `False` |

### Правила и ошибки

Сравнение и удаление атомарны.

## <span className="operation-badge operation-badge--cache">CACHE</span> `write_cached_answer_if_not_older` {#write-cached-answer-if-not-older}

~~~python
async def write_cached_answer_if_not_older(query: str, value: CachedAnswer) -> bool: ...
~~~

**Кто вызывает.** Exact Cache.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Нормализованный вопрос из Guardrails. | `"Где инструкция по установке?"` |
| `value` | `CachedAnswer` | Сохранённый результат и поколение, по которому он получен. | `{"corpus_generation": 42, "answer_text": "См. раздел «Установка».", "sources": [...]}` |

### Что отдаёт

**Формат.** Одно логическое значение.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `bool` | Значение записано с TTL. | `True` |
| `return` | `bool` | В ключе уже есть ответ более нового поколения. | `False` |

### Правила и ошибки

Сравнение и запись атомарны.

## DTO

~~~python
from pydantic import AwareDatetime, Field
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText
from dataclasses import dataclass

class CachedAnswer(RagResult):
    corpus_generation: NonNegativeInt


class CacheRead(DTO):
    value: CachedAnswer
    compare_token: bytes
~~~

[`RagResult`](../../rag-core/contracts.md#общие-dto) описан в общих DTO.
