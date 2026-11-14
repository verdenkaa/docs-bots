---
title: "Точка входа Searcher"
sidebar_position: 2
toc_max_heading_level: 2
---

[Правила DTO и Pydantic](../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.



## <span className="operation-badge operation-badge--async">ASYNC</span> `search` {#search}

~~~python
async def search(query: str, revision_ids: frozenset[UUID]) -> list[SearchFragment]: ...
~~~

**Кто вызывает.** RAG Core при промахе Exact Cache.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Нормализованный вопрос из Guardrails. | `"Где инструкция по установке?"` |
| `revision_ids` | `frozenset[UUID]` | Редакции из load_revision_ids для закреплённого поколения. | `frozenset({UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")})` |

### Что отдаёт

**Формат.** Список объектов Pydantic `SearchFragment`, отсортированных по релевантности. `[]` означает, что кандидатов нет. `[]` в колонке поля обозначает элемент списка.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `[].chunk_id` | `UUID` | Стабильный ID чанка. Генератор ссылается на него. | `UUID("545f64ff-7692-579a-842a-31f87e6522df")` |
| `[].document_id` | `UUID` | ID документа во всех его редакциях. | `UUID("43f95f66-7343-4cbc-beb5-19fd36a8ca77")` |
| `[].revision_id` | `UUID` | Редакция, из которой взят чанк. | `UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")` |
| `[].text` | `str` | Текст для контекста генератора. | `"Фрагмент инструкции по установке."` |
| `[].source_name` | `str` | Название источника для пользователя. | `"Руководство по установке.pdf"` |
| `[].locator` | `SourceLocator \| None` | Место в документе, если его можно указать. | `{"page": 4, "section": "Установка"}` |
| `[].source_url` | `str \| None` | В MVP None: API строит ссылку на HTML-заглушку документа. | `None` |
| `[].images` | `list[ImageRef]` | ID/описания изображений этого чанка из payload. | `[]` |

Фрагмент также возвращает images: list[ImageRef] из payload; без изображений это []. Источник ID — Gateway, не Searcher.

### Правила и ошибки

При пустом наборе редакций возвращает `[]` без обращения к моделям и Qdrant. Для вопроса со ссылкой явно передаёт lexical_priority=True в find_chunks; численные веса берёт из профиля. Примеры topic_questions не ищет и не возвращает. Модельные операции выполняет через Model Gateway: `embed_texts` даёт `EmbeddingResult`, опциональный `rerank` — `RerankResult`; Searcher проверяет профили и возвращает Core только фрагменты. При сбое поднимает `StepError(code, retryable)`. [DTO фрагмента](../rag-core/contracts.md#общие-dto) · [Конвейер](./pipeline.md).

## Пример полного результата

Ниже — данные для валидации; внутри Python Searcher возвращает список объектов `SearchFragment`. [Правила DTO](../faq/dto.md).

~~~python
[
    {
        "chunk_id": UUID("545f64ff-7692-579a-842a-31f87e6522df"),
        "document_id": UUID("43f95f66-7343-4cbc-beb5-19fd36a8ca77"),
        "revision_id": UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139"),
        "text": "Фрагмент инструкции по установке.",
        "source_name": "Руководство по установке.pdf",
        "locator": {"page": 4, "section": "Установка"},
        "source_url": None,
    }
]
~~~
