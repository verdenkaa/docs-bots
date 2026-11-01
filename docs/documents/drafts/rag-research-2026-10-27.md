---
title: "Исследование №1 — PostgreSQL + Qdrant"
description: "Конспект архитектурного отчёта от 27 октября 2026"
sidebar_position: 3
---

# Исследование №1: PostgreSQL + Qdrant

> **27 октября 2026 · редакция 2.0 · статус: предложение, не утверждено.** Полное ТЗ и корпус БАРС Груп неизвестны. Численные цели не проверены на реальной системе.

## За две минуты

**Рекомендация исследования:** модульный Python-монолит; PostgreSQL — авторитетное хранилище публикаций, ACL и jobs; Qdrant — dense + lexical retrieval; Redis — необязательный exact answer cache; отдельный ingestion worker; разрешённая внешняя LLM.

**Ключевой аргумент:** специализированный поиск Qdrant плюс одна SQL-транзакция для выбора опубликованного, заранее полностью проверенного индекса. **Не доказано**, что этот вариант быстрее или качественнее PostgreSQL + pgvector.

**Для победы:** показывать не число используемых технологий, а достоверный ответ с цитатой, обновление документации под нагрузкой без смешения версий и честные p95 для повторных и уникальных вопросов.

| Область | Выбор | Условие пересмотра |
| --- | --- | --- |
| Search | Qdrant dense + lexical, RRF | Corpus-specific Recall@k/latency окажутся хуже SQL |
| Freshness | Immutable snapshot, PostgreSQL publication epoch | Частые правки и дорогой full rebuild |
| Cache | Embedding reuse P0; Redis exact answers P1 | Низкая доля повторов или unsafe scope keys |
| Reranking | По умолчанию выключен | Доказанное улучшение gold set |
| LLM | Один grounded вызов модели | API/квота/доступ/качество требуют альтернативы |

[Обзор двух исследований](./rag-research-overview.md) · [Исследование №2](./rag-research-2026-11-01.md)

---

## Архитектура и обработка запроса

~~~mermaid
flowchart TB
  T[Telegram] --> G[Identity and durable inbox]
  G --> A[Bounded admission and deadline]
  A --> P[Pin PostgreSQL snapshot and ACL]
  P --> C{Redis exact answer cache}
  C -->|hit| V[Validate citation and version]
  C -->|miss| Q[Qdrant hybrid: dense plus lexical RRF]
  Q --> E[Evidence selection, optional rerank]
  E --> M[Grounded LLM with source IDs]
  M --> V
  V --> S[Final ACL and expiry gate]
  S --> T
~~~

Входящий update сохраняется и дедуплицируется перед webhook ACK. После очереди фиксируются publication epoch, физическая коллекция и область доступа. Redis hit разрешён только при совпадении вопроса, сессии, истории, scope, knowledge/authz epochs, версии prompt/model/retriever. Cache miss идёт в Qdrant по закреплённому снимку. Модель генерирует короткие claims с идентификаторами источников, а сервер проверяет membership, права, версию, quote/locator и формирует ссылки. Проверка ID не доказывает semantic entailment, поэтому нужен gold-set и отказы при недостатке свидетельств.

## Публикация и обновление документов

~~~mermaid
flowchart LR
  U[Upload] --> I[Parse, chunk, embed]
  I --> Q[New Qdrant staging collection]
  Q --> R[Readiness and completeness checks]
  R --> PG[PostgreSQL CAS publish, epoch plus one]
  PG --> N[New requests pin new epoch]
  PG --> GC[Retain old version then GC]
~~~

**Одна публикация — один commit.** Индекс готовится в новой физической коллекции и становится immutable после проверки. SQL транзакция переключает active snapshot и монотонный epoch. Запрос, уже закрепивший предыдущий snapshot, может завершить его с указанной версией; новый запрос после commit видит новую. Rollback увеличивает epoch ещё раз. Это согласованность **известного опубликованного** состояния, а не гарантия обнаружения внешнего изменения в момент его появления. Для экстренного revoke/удаления секрета требуется независимый deny/send gate.

Стоимость: две коллекции, rebuild, GC и хранение originals. Embeddings неизменившихся чанков переиспользуются по хешу полного encoder input, но I/O не исчезает. При большом корпусе или frequent churn нужен тест incremental/COW.

## Поиск, качество и документы

Основной кандидат — hybrid dense + sparse/BM25/RRF. Технические версии, команды, коды ошибок и отрицания требуют лексического или точного поиска, перефразировки — dense. Проверять одинаковые retrieval fixtures на BM25-only, dense-only и hybrid; reranker подключать лишь при измеренном выигрыше.

Ingestion сохраняет структуру заголовков, таблиц с header/units, code blocks, страницу/раздел и raw evidence. Нельзя считать отсутствие text layer в сканированном PDF успешным извлечением; нужен OCR или quarantine. Chunking структурный, с breadcrumbs и provenance. Citation membership, correctness, evidence sufficiency и refusal — разные измерения.

**Показатели проверки:** Recall@k, ответ на hard negatives и отсутствующие данные, claim correctness, citation precision/coverage, freshness races, ACL violations, p50/p95/full miss/hit, tokens/cost.

## Производительность и кеш

Exact-cache hit убирает наиболее дорогой путь (query embedding + retrieval + LLM), но TTL не является механизмом корректной инвалидации. Versioned namespace предотвращает использование старого ответа новым epoch. Singleflight полезен для одновременно одинаковых вопросов; ни Redis, ни singleflight не увеличивают RPM/TPM для **уникальных** запросов.

Для новых запросов: один LLM call, компактный evidence, устранение дублирующих chunks, короткий output, bounded queues/admission по токенам и запросам, отсутствие обязательных query rewrite/agent/LLM judge. Provider prefix cache уменьшает prefill, но не decode. Не путать KV-cache собственного inference с Redis кешем ответов.

**Результаты экспериментов:** в исходном пакете заявлены восемь пройденных тестов упрощённой Python consistency-модели; это не PostgreSQL isolation, Qdrant integration или RAG-бенчмарк. Реальные p95, Recall@k, качество ответов, quota и end-to-end Telegram latency не измерены. Дополнительное исследование unique-question latency рассмотрело 17 научных работ и 6 первичных материалов; его протоколы также NOT RUN.

## Рассмотренные архитектурные альтернативы

| | Плюсы | Минусы |
| --- | --- | --- |
| PostgreSQL + pgvector + FTS | Единый control/search store, меньше сервисов | Настройка FTS/fusion и filtered ANN |
| **PostgreSQL + Qdrant (предпочтение)** | Готовые hybrid primitives, независимый retrieval | Координация индекса и SQL pointer |
| Distributed workers/inference | Возможность масштабировать bottlenecks | Существенная интеграционная сложность за 48 ч |

План исследования: сначала сквозной запрос и две версии документа; затем retrieval/gold, нагрузка и exact-cache; после этого failure tests и заморозка функций. Не пытаться строить GraphRAG, semantic answer cache, сложную HA до доказанного P0.

## Открытые вопросы

1. Обязательны ли Qdrant, Redis, PostgreSQL и определённая LLM?
2. Каковы реальные объём и форматы документов, нужна ли OCR и обработка таблиц?
3. Что значит актуальность: при admit, после publish или перед Telegram send?
4. Какие права пользователей, tenants и сроки revoke?
5. Какие quota, стоимость, политика отправки текстов во внешнее API?
6. Что считается нагрузкой — cached или unique questions, backend или Telegram delivery?

## Основные первоисточники

[Qdrant hybrid](https://qdrant.tech/documentation/search/hybrid-queries/) · [Qdrant collections](https://qdrant.tech/documentation/manage-data/collections/) · [PostgreSQL isolation](https://www.postgresql.org/docs/18/transaction-iso.html) · [BEIR](https://arxiv.org/abs/2104.08663) · [RRF](https://plg.uwaterloo.ca/~gvcormac/cormacksigir09-rrf.pdf) · [ALCE](https://aclanthology.org/2023.emnlp-main.398/) · [RAGChecker](https://arxiv.org/abs/2408.08067) · [RAGCache](https://arxiv.org/html/2404.12457v2) · [CacheBlend](https://arxiv.org/html/2405.16444v3).

**Происхождение:** обзор на основе полного отчёта, матриц, Team Brief и дополнительного unique-latency исследования из архива BARS_RAG_Research_Package.zip. Это конспект для сайта, не копия полного исходного 15-раздельного отчёта.
