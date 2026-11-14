---
title: "Точка входа Guardrails"
sidebar_position: 2
toc_max_heading_level: 2
---

## <span className="operation-badge operation-badge--async">ASYNC</span> `check_question` {#check-question}

~~~python
async def check_question(raw_question: str, revision_ids: frozenset[UUID]) -> GuardrailsResult: ...
~~~

**Кто вызывает.** Core после текста/OCR и load_revision_ids, до кеша.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| raw_question | str | Исходный текст либо сохранённый prepared_text. | Ошибка AUTH-17 |
| revision_ids | frozenset[UUID] | Редакции закреплённого поколения для примеров темы. | frozenset(&#123;UUID(...)&#125;) |

### Что отдаёт

**Формат.** Один вариант GuardrailsResult.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| kind / normalized_question | allowed / str | Допуск и непустой текст для остальных шагов. | "allowed" / "Где фильтр?" |
| kind | blocked / invalid_input | Инъекция или явная бессмыслица. | "invalid_input" |
| kind | out_of_scope | Низкое сходство с примерами темы. | "out_of_scope" |
| kind / retryable | detector_unavailable / bool | Технический сбой обязательной проверки. | "detector_unavailable" / True |

### Правила и ошибки

Сначала проверяет ввод/явную бессмыслицу своим простым классификатором, затем инъекцию через classify_injection; спорный осмысленный текст не отбрасывает только по этой проверке. Нормализует, сохраняя URL/числа/операторы. При непустых revision_ids получает embed_texts([query],"query",...) и find_topic_matches; по щадящему порогу решает тему. При непустых revision_ids успешная пустая выдача примеров означает неполноту индекса и detector_unavailable, а не out_of_scope. Порог применяется в Guardrails после получения ближайшего примера без серверного score_threshold. Пустой revision_ids не вызывает Qdrant и после базовых проверок даёт allowed: Core знает, что корпус пуст, и создаёт SupportOutcome(no_evidence). Сбой индекса/модели даёт detector_unavailable. Модельное изменение смысла после проверки не допускается.
