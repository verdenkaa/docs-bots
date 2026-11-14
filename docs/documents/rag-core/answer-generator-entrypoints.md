---
title: "Точка входа Answer Generator"
sidebar_position: 8
toc_max_heading_level: 2
---

[Правила DTO и Pydantic](../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.



## <span className="operation-badge operation-badge--async">ASYNC</span> `generate_answer` {#generate-answer}

~~~python
async def generate_answer(query: str, fragments: Sequence[SearchFragment]) -> GenerationResult: ...
~~~

**Кто вызывает.** RAG Core после получения непустой выдачи Searcher.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Нормализованный вопрос из Guardrails. | `"Где инструкция по установке?"` |
| `fragments` | `Sequence[SearchFragment]` | Упорядоченные фрагменты с текстом и метаданными для контекста. | `[{"chunk_id": UUID("545f64ff-7692-579a-842a-31f87e6522df"), "text": "Фрагмент инструкции по установке.", ...}]` |

### Что отдаёт

**Формат.** Один объект Pydantic. `GenerationResult` объединяет два варианта по полю `kind`.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `kind` | `Literal["answered"]` | Модель сформировала ответ по контексту. | `"answered"` |
| `answer_text` | `str` | Непустой текст. Только для answered. | `"См. раздел «Установка»."` |
| `cited_chunk_ids` | `list[UUID]` | Непустой список ID использованных чанков. Только для answered. | `[UUID("545f64ff-7692-579a-842a-31f87e6522df")]` |
| `cited_image_ids` | `list[UUID]` | ID изображений из реально переданного контекста; по умолчанию []. Совпадают с плейсхолдерами answer_text. | `[]` |
| `kind` | `Literal["insufficient_evidence"]` | Контекста недостаточно, других полей нет. | `"insufficient_evidence"` |

### Правила и ошибки

Внутри вызывает `Model Gateway.generate`; `ModelGenerationResult` не выходит в Core напрямую. Только `completed` с валидным бизнес-JSON преобразуется в `GenerationResult`; `length`/`filtered` дают `StepError("generation_incomplete", False)`. При сбое модели или нарушении формата поднимает `StepError(code, retryable)`. Core собирает источники и ImageRef по ID из выдачи; неизвестный ID либо несовпадение маркеров — StepError. [DTO](./contracts.md#исходы-шагов) · [Сборка контекста](./answer-generator.md).

Все cited_image_ids должны происходить из включённых в контекст и процитированных чанков. ID в плейсхолдерах Markdown совпадают с этим набором, повторяющиеся вхождения одного изображения не создают несколько ImageRef. Нет подтверждений — insufficient_evidence; сбой модели/формата — StepError. [Бизнес-правила](./answer-generator.md).
