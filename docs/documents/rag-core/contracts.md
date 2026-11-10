---
title: Общие DTO
sidebar_position: 3
---

:::warning Рабочие контракты
Типы фиксируют первый сквозной сценарий. Формат локатора расширим, когда станут известны типы источников.
:::

## Очередь и готовый результат

Оба маршрута, `rag.questions` и `rag.results`, передают один DTO `RequestRef` в JSON. Маршрут определяет действие. RAG API пишет исходный текст и чат в `rag_requests`. RAG Core пишет итог в `rag_requests.result`. Для доставки RAG API читает `chat_id` и `RagResult` по тому же `request_id`. [Формат RabbitMQ](../infrastructure/rabbitmq/index.md) · [Строка запроса в PostgreSQL](../infrastructure/postgresql/index.md#rag_requests)

~~~json
{ "request_id": 1042 }
~~~

`ack` означает, что уведомление можно подтвердить. `retry_broker` означает, что надёжный переход не удалось проверить или сохранить в PostgreSQL. Адаптер оставляет возможность повторной доставки RabbitMQ с задержкой. Если Core уже сохранил прикладной повтор в outbox, текущее уведомление получает `ack`.

## Общие DTO

~~~python
from collections.abc import Sequence
from datetime import datetime
from typing import Literal, TypedDict
from uuid import UUID


class RequestRef(TypedDict):
    request_id: int


ConsumeDecision = Literal["ack", "retry_broker"]


class ProcessingAttempt(TypedDict):
    request_id: int
    attempt_no: int
    deadline_at: datetime


class ClaimedQuestion(TypedDict):
    attempt: ProcessingAttempt
    raw_question: str
    corpus_generation: int


class SourceLocator(TypedDict, total=False):
    page: int
    section: str


class SourceRef(TypedDict):
    chunk_id: UUID
    document_id: UUID
    revision_id: UUID
    source_name: str
    locator: SourceLocator | None
    source_url: str | None


class SearchFragment(SourceRef):
    text: str


class RagResult(TypedDict):
    answer_text: str
    sources: list[SourceRef]
~~~

- `raw_question` берём из `question_text`. `corpus_generation` — сохранённый `pinned_generation`, а `deadline_at` — срок активной попытки из `resume_at` в UTC. `attempt_no` отсчитывается с 1.
- `chunk_id` соответствует ID точки Qdrant. Остальные поля источника приходят из payload. Адаптер приводит отсутствующие `locator` и `source_url` к `None`. При сохранении UUID становятся строками JSON, `None` — `null`. [Пример точки](../infrastructure/qdrant/index.md#структура-точки)
- `RagResult` хранит готовый непустой текст и упорядоченные источники. Для ответа по документам список источников непустой. Для просьбы уточнить вопрос, отсутствия подтверждения или технического сообщения используем `sources: []`. Текст чанков нужен в памяти на этапе генерации. В PostgreSQL и Exact Cache сохраняем только `RagResult` и служебные данные соответствующего хранилища.

## Исходы шагов

~~~python
class AllowedQuestion(TypedDict):
    kind: Literal["allowed"]
    normalized_question: str


class RejectedQuestion(TypedDict):
    kind: Literal["blocked", "invalid_input"]


class DetectorUnavailable(TypedDict):
    kind: Literal["detector_unavailable"]
    retryable: bool


GuardrailsResult = AllowedQuestion | RejectedQuestion | DetectorUnavailable


class GeneratedAnswer(TypedDict):
    kind: Literal["answered"]
    answer_text: str
    cited_chunk_ids: list[UUID]


class InsufficientEvidence(TypedDict):
    kind: Literal["insufficient_evidence"]


GenerationResult = GeneratedAnswer | InsufficientEvidence


class StepError(Exception):
    code: str
    retryable: bool


class StorageError(Exception):
    ...
~~~

`normalized_question` существует только при допуске и содержит непустую строку. `cited_chunk_ids` при `answered` содержит хотя бы один ID из переданных фрагментов. Searcher возвращает упорядоченный список с уникальными `chunk_id`. Пустой список и `insufficient_evidence` ведут к одному сообщению о недостатке данных.

Searcher и Answer Generator поднимают `StepError`. Код нужен для диагностики, а `retryable` определяет, можно ли повторить обработку. Runtime поднимает `StorageError`, если чтение или переход в PostgreSQL подтвердить не удалось. Ingestion использует тот же тип для сбоя доступа к временному тому и обрабатывает его отдельно от сбоя PostgreSQL. Ошибки Redis обрабатывает сам Exact Cache.

## Сигнатуры вызовов

- [RAG Core](./entrypoints.md) · [Guardrails](../guardrails/entrypoints.md) · [Searcher](../retrieval/entrypoints.md)
- [Exact Cache](./exact-cache-entrypoints.md) · [Answer Generator](./answer-generator-entrypoints.md) · [RAG API](../telegram/module/entrypoints.md)
- [Ingestion](../ingestion/entrypoints.md) · [PostgreSQL для RAG Core](../infrastructure/postgresql/rag-core.md)

`query` в вызовах после Guardrails всегда означает `normalized_question` из `AllowedQuestion`. `retry_at` задаётся в UTC. Для переходов PostgreSQL `True` подтверждает коммит, `False` означает, что попытка уже утратила право на запись. Неизвестный итог транзакции обрабатываем как ошибку хранилища и повторно проверяем состояние при доставке. [Порядок обработки](./flow.md)

[Контракт Ingestion](../ingestion/contracts.md) описывает отдельный вход загрузки и `JobRef`. [Функции PostgreSQL](../infrastructure/postgresql/index.md#что-вызывать) связывают обработчики обоих потоков с состоянием и outbox.
