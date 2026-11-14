---
title: "Контракты Redis"
sidebar_position: 2
toc_max_heading_level: 2
---

[Правила DTO и Pydantic](../../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.

Функции строят ключ из нормализованного вопроса. Все три операции при недоступности Redis или превышении таймаута поднимают `CacheUnavailable`. Exact Cache преобразует сбой чтения в промах, а сбой необязательного удаления или записи не прерывает обработку вопроса. Отсутствующее или некорректное значение при чтении возвращается как `None`. [Формат хранения](./index.md).

## Ошибка доступности

Публичный импорт: `from rag.runtime.redis import CacheUnavailable`.

~~~python
class CacheUnavailable(Exception):
    """Операция Redis недоступна; Exact Cache допускает работу без кеша."""
~~~

Адаптер преобразует ошибки доступности клиентской библиотеки Redis в этот тип, сохраняя причину через `raise CacheUnavailable(...) from exc`. Ошибки программирования не маскируются общим `except Exception`.

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

**Формат.** Объект Pydantic `CacheRead` или `None`.

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
CachedAnswer наследует также images из RagResult. При чтении/записи поле не отбрасывается; неизвестная старая схема кеша даёт промах/инвалидацию, не потерю картинок.
