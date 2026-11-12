---
title: "Точки входа Exact Cache"
sidebar_position: 6
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Контракты заданы для MVP. Лимиты и настройки запуска уточняются при реализации.
:::

[Правила DTO и Pydantic](../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.



## <span className="operation-badge operation-badge--cache">CACHE</span> `get_answer` {#get-answer}

~~~python
async def get_answer(query: str, corpus_generation: int) -> RagResult | None: ...
~~~

**Кто вызывает.** RAG Core после допуска Guardrails.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Нормализованный вопрос из Guardrails. | `"Где инструкция по установке?"` |
| `corpus_generation` | `int` | Поколение, закреплённое в PostgreSQL за этим запросом. | `42` |

### Что отдаёт

**Формат.** Объект Pydantic `RagResult` или `None` при промахе, несовпадении поколения или сбое Redis.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `answer_text` | `str` | Готовый текст ответа. | `"См. раздел «Установка»."` |
| `sources` | `list[SourceRef]` | Источники в порядке использования. | `[{"chunk_id": UUID("545f64ff-7692-579a-842a-31f87e6522df"), ...}]` |
| `return` | `None` | В кеше нет подходящего результата. | `None` |

### Правила и ошибки

При совпадении поколения возвращаются только поля результата. [DTO RagResult](./contracts.md#общие-dto).

## <span className="operation-badge operation-badge--cache">CACHE</span> `store_answer` {#store-answer}

~~~python
async def store_answer(query: str, corpus_generation: int, result: RagResult) -> None: ...
~~~

**Кто вызывает.** RAG Core после успешного коммита нового ответа по документам.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Нормализованный вопрос из Guardrails. | `"Где инструкция по установке?"` |
| `corpus_generation` | `int` | Поколение, закреплённое в PostgreSQL за этим запросом. | `42` |
| `result` | `RagResult` | Текст ответа и непустой список источников, уже сохранённые в PostgreSQL. | `{"answer_text": "См. раздел «Установка».", "sources": [...]}` |

### Что отдаёт

**Формат.** Одно значение Python, `None`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `None` | Операция завершилась. Запись выполняется по возможности. | `None` |

### Правила и ошибки

Ошибка Redis фиксируется в диагностике. [Правила кеша](./exact-cache.md) · [Функции Redis](../infrastructure/redis/contracts.md).
