---
title: "Точка входа Guardrails"
sidebar_position: 2
toc_max_heading_level: 2
---

:::warning Рабочий контракт
Модель, обучающие данные и ограничения длины уточняются.
:::

Для реализации используется `check_question`. [Место вызова в RAG Core](../rag-core/flow.md#порядок-вызовов-и-происхождение-данных).

## <span className="operation-badge operation-badge--async">ASYNC</span> `check_question` {#check-question}

~~~python
async def check_question(raw_question: str) -> GuardrailsResult: ...
~~~

**Кто вызывает.** RAG Core после `claim_question`, до Exact Cache.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| `raw_question` | `str` | Исходный текст из PostgreSQL для проверки и нормализации. Пустая строка или только пробелы дают invalid_input. | `"Где инструкция по установке?"` |

### Что отдаёт

**Формат.** Один словарь Python. `GuardrailsResult` объединяет четыре варианта по полю `kind`. Дополнительные поля присутствуют только в указанном варианте.

| Поле или значение | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `kind` | `Literal["allowed"]` | Вопрос допущен. | `"allowed"` |
| `normalized_question` | `str` | Только при allowed. Непустой текст для кеша, поиска и генерации. | `"Где инструкция по установке?"` |
| `kind` | `Literal["blocked"]` | Сработал детектор. Core просит переформулировать вопрос. | `"blocked"` |
| `kind` | `Literal["invalid_input"]` | Ввод не прошёл проверку. Core просит прислать корректный вопрос. | `"invalid_input"` |
| `kind` | `Literal["detector_unavailable"]` | Проверка не завершилась из-за технического сбоя. | `"detector_unavailable"` |
| `retryable` | `bool` | Только при detector_unavailable. Core использует признак, чтобы решить, повторять ли запрос. | `True` |

### Правила и ошибки

Успех выглядит как `{"kind": "allowed", "normalized_question": "Где инструкция по установке?"}`. Отказ выглядит как `{"kind": "blocked"}`. Сбои детектора преобразуются в `detector_unavailable`. [Определение DTO](../rag-core/contracts.md#исходы-шагов) · [Правила нормализации](./index.md#нормализация).
