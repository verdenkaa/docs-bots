---
title: DTO и ошибки Model Gateway
sidebar_position: 3
---

:::warning Рабочий контракт
Структуры фиксируют границу модулей. Значения профилей, пороги, провайдеры и численные лимиты выберем по кейсу; адаптеры ещё не реализованы.
:::

DTO — объекты Pydantic из `app/rag/model_gateway/contracts.py`. Все перечисленные поля обязательны; `None` передаём явно. Транспортный JSON конкретного провайдера наружу не выходит. [Точки входа](./entrypoints.md).

Python-блоки задают точные типы, ограничения и валидаторы; тесты сверяют их с кодом. Таблицы поясняют назначение полей и показывают примеры. Общие ограничения `DTO`, `PositiveId`, `NonNegativeInt` и `NonEmptyText` описаны в [правилах DTO](../faq/dto.md#база-и-ограничения).

## Конфигурация роли {#binding}

Runtime передаёт `Mapping[ModelRole, ModelBinding]` в `start_models`. Ключ задаёт роль, значение — один адаптер. Worker требует `embedding`; Core — `embedding`, `injection`, `generation` и `reranker`, если он включён в профиле поиска.

~~~python
from typing import Annotated, Literal, Self
from pydantic import Field, model_validator
from rag.dto import DTO, PositiveId, NonNegativeInt, NonEmptyText


ModelRole = Literal["embedding", "reranker", "injection", "generation"]


class ModelBinding(DTO):
    profile_id: NonEmptyText
    execution: Literal["local", "remote"]
    provider: NonEmptyText
    model_id: NonEmptyText
    model_revision: NonEmptyText
    endpoint: str | None
    credential_env: str | None
    max_concurrency: PositiveId
    embedding_dimensions: PositiveId | None

    @model_validator(mode="after")
    def check_binding(self) -> Self:
        if self.execution == "remote" and not self.endpoint:
            raise ValueError("remote binding requires endpoint")
        return self
~~~

| Поле ModelBinding | Смысл | Пример |
| --- | --- | --- |
| `profile_id` | Непустой ID неизменяемого профиля: модель, версия, preprocessing, инструкции query/document и параметры вычисления. | `"embedding-v1"` |
| `execution` | Локальное вычисление в процессе либо сетевой вызов, включая собственный сервер модели. | `"remote"` |
| `provider` | Ключ зарегистрированного адаптера. Здесь нет выбранного по умолчанию провайдера. | `"example_embeddings_api"` |
| `model_id` | ID модели или deployment у провайдера. | `"case-embedding"` |
| `model_revision` | Закреплённая версия весов/deployment. Если API не позволяет её закрепить, ограничение явно фиксируем до допуска профиля. | `"revision-1"` |
| `endpoint` | Адрес удалённого сервера; для локальной модели — `None`. | `"http://embedding:8080"` |
| `credential_env` | Имя переменной окружения с секретом; значение читается внутри адаптера и не попадает в DTO/логи. | `"EMBEDDING_API_KEY"` |
| `max_concurrency` | Положительный предел одновременных вычислений/сетевых вызовов в этом процессе. Общую квоту сервера настраиваем также на стороне сервера. | `2` |
| `embedding_dimensions` | Положительная размерность только для embedding; для остальных ролей — `None`. | `1024` |

Одинаковый `profile_id` не может означать разные настройки. Для query и document используется один embedding-профиль, но адаптер применяет соответствующие роли инструкции. Размерности недостаточно для совместимости: при смене модели или правил кодирования нужна новая конфигурация индекса и переиндексация. До реализации фиксируем профиль в конфигурации развёртывания; автоматическая смена профиля на работающем корпусе не поддерживается.

## Эмбеддинги {#embedding-result}

~~~python
class EmbeddingResult(DTO):
    profile_id: NonEmptyText
    vectors: list[list[float]]
~~~

| Поле EmbeddingResult | Смысл | Пример |
| --- | --- | --- |
| `profile_id` | Профиль реально выполненного кодирования. | `"embedding-v1"` |
| `vectors` | Один вектор на текст, в исходном порядке; все координаты конечны, размерность соответствует binding. | `[[0.1, -0.2, ...]]` |

## Reranker {#rerank-result}

~~~python
class RerankScore(DTO):
    candidate_index: NonNegativeInt
    score: float


class RerankResult(DTO):
    profile_id: NonEmptyText
    scores: list[RerankScore]
~~~

| Поле RerankResult | Смысл | Пример |
| --- | --- | --- |
| `profile_id` | Профиль reranker. | `"reranker-v1"` |
| `scores` | По записи на каждый входной кандидат; порядок исходных индексов. | `[{"candidate_index": 0, "score": 2.4}]` |
| `scores[].candidate_index` | Индекс в переданном `candidates`, от 0; без пропусков и повторов. | `0` |
| `scores[].score` | Конечная оценка: больше — релевантнее. Не обязана быть вероятностью или лежать в `[0, 1]`. | `2.4` |

Gateway не отсекает кандидатов. Searcher сортирует по убыванию оценки, при равенстве сохраняет исходный порядок, затем применяет свой порог и `top_k`.

## Детектор инъекций {#injection-result}

~~~python
class InjectionResult(DTO):
    profile_id: NonEmptyText
    label: Literal["benign", "injection"] | None
    score: float | None

    @model_validator(mode="after")
    def require_observation(self) -> Self:
        if self.label is None and self.score is None:
            raise ValueError("label or score is required")
        return self
~~~

| Поле InjectionResult | Смысл | Пример |
| --- | --- | --- |
| `profile_id` | Профиль детектора, с которым согласованы правила Guardrails. | `"injection-v1"` |
| `label` | Нативный класс модели, приведённый к общему названию. `None`, если есть только score. | `None` |
| `score` | Конечная оценка, больше — выше риск инъекции. Шкала зависит от профиля и не считается вероятностью. | `0.8` |

Хотя бы одно из `label`/`score` заполнено. Адаптер не придумывает порог для получения класса из score. Guardrails для закреплённого профиля явно выбирает использование нативного класса либо score и порога; при наличии обоих полей правило приоритета фиксируется там же. До настройки правила профиль не готов к рабочему запуску. Непригодный ответ — ошибка контракта, а не `benign`.

## Генерация {#generation}

~~~python
class ModelMessage(DTO):
    role: Literal["system", "user", "assistant"]
    content: NonEmptyText


class GenerationOptions(DTO):
    max_output_tokens: PositiveId
    temperature: Annotated[float, Field(ge=0)] | None


class TokenUsage(DTO):
    input_tokens: NonNegativeInt | None
    output_tokens: NonNegativeInt | None


class ModelGenerationResult(DTO):
    profile_id: NonEmptyText
    text: str
    finish_reason: Literal["completed", "length", "filtered"]
    usage: TokenUsage | None

    @model_validator(mode="after")
    def require_completed_text(self) -> Self:
        if self.finish_reason == "completed" and not self.text.strip():
            raise ValueError("completed generation requires text")
        return self
~~~

| DTO / поле | Смысл | Пример |
| --- | --- | --- |
| `ModelMessage.role` | Роль сообщения. Адаптер сохраняет разделение инструкций и пользовательских данных либо отклоняет неподдерживаемую конфигурацию. | `"system"` |
| `ModelMessage.content` | Непустой текст сообщения. | `"Отвечай по предоставленным источникам."` |
| `GenerationOptions.max_output_tokens` | Положительный предел вывода; не включает бюджет входного контекста. | `512` |
| `GenerationOptions.temperature` | Неотрицательное значение в допустимом диапазоне модели; `None` использует закреплённую настройку профиля. | `0.0` |
| `ModelGenerationResult.profile_id` | Профиль генерации. | `"generation-v1"` |
| `ModelGenerationResult.text` | Собранный текст ответа. Может быть пустым при фильтрации; в `completed` непустой. | `"{\"kind\": \"insufficient_evidence\"}"` |
| `ModelGenerationResult.finish_reason` | Завершённый ответ / исчерпан лимит вывода / фильтрация провайдером. | `"completed"` |
| `ModelGenerationResult.usage` | Статистика провайдера; `None`, если не предоставлена. | `{"input_tokens": 700, "output_tokens": 120}` |
| `TokenUsage.input_tokens` | Неотрицательное число входных токенов или неизвестно. | `700` |
| `TokenUsage.output_tokens` | Неотрицательное число выходных токенов или неизвестно. | `120` |

`ModelGenerationResult` — сырой результат вызова модели. Он **не заменяет** бизнес-DTO [`GenerationResult`](../rag-core/contracts.md#исходы-шагов): JSON, метки `S1` и ID чанков проверяет Answer Generator. Неизвестную причину завершения считаем ошибкой адаптера, а `length`/`filtered` не превращаем в полноценный ответ.

## Ошибки {#errors}

Во всех операциях технические ошибки представлены общим `StepError(code, retryable)` из `rag.contracts`. Детали SDK остаются внутри адаптера. Отмена вызывающей корутины пробрасывается как отмена, а не как успешный результат или самостоятельный повтор.

| `code` | `retryable` | Смысл |
| --- | --- | --- |
| `model_invalid_input` | `False` | Неверный ввод, неположительный timeout или неподдерживаемые параметры. |
| `model_not_ready` | `False` | Роль не настроена либо запуск ещё не завершён. В рабочем профиле это ошибка сборки. |
| `model_profile_mismatch` | `False` | Профиль/размерность не совпадают с ожидаемыми. |
| `model_invalid_response` | `False` | Неполные векторы, неконечные числа, неверные индексы или другой нарушенный DTO. |
| `model_configuration_error` | `False` | Неверный адрес, ключ, версия или несовместимая конфигурация. |
| `model_timeout` / `model_overloaded` / `model_unavailable` | `True` | Временная недоступность, исчерпан бюджет ожидания/вызова либо очередь допуска заполнена. |

Известный постоянный отказ API не маркируем временным. Повтор всего вопроса назначает RAG Core, задания — Ingestion Worker. Gateway не запускает скрытые циклы повторов и не переключает провайдера.
