---
title: "Исследование №2 — независимые архитектурные альтернативы"
description: "Независимое исследование от 1 ноября 2026: PG-only, late interaction и long-context"
sidebar_position: 4
---

# Исследование №2: архитектура RAG с чистого листа

> **1 ноября 2026 · статус: независимое исследовательское предложение.** Новая работа не использовала первое архитектурное исследование как входной материал; исходные критерии — качество ответа, актуальность, скорость повторов и скорость уникальных запросов. Полное ТЗ, корпус, модели/квоты и hardware неизвестны. Никаких измерений итоговой системы нет.

## За две минуты

**Предварительная рекомендация — вариант A: PostgreSQL + pgvector + PostgreSQL FTS и разрешённая внешняя LLM.** Объяснение — меньше сервисов, единая транзакционная граница публикаций, меньше риск интеграции за ~48 часов. Это **не доказательство** превосходства по Recall@k или p95. Сменить вариант после короткой проверки документов, GPU и API.

| Вариант | Преимущество | Когда выбирать |
| --- | --- | --- |
| **A. Transactional hybrid (предпочтение)** | SQL хранит ревизии, поиск и указатель публикации вместе | Обычный сервер, доступный внешний API, неизвестный корпус |
| **B. Local late-interaction** | Qdrant dense/sparse + token-level MaxSim, локальный vLLM без egress | Доступная GPU, ограничения внешнего API, доказанный выигрыш retrieval |
| **C. Long-context / CAG** | Малый корпус целиком в контексте модели, reuse стабильного префикса | Корпус помещается, cold unique latency и качество проходят тест |

**Главное отличие от исследования №1:** строгая модель freshness: после обнаруженной правки можно временно блокировать старую редакцию; перед dispatch проверять generation/ACL/pending changes; при изменении — один повтор или явный временный отказ. Это дороже по availability и latency, зато снижает число stale sends.

[Обзор двух исследований](./rag-research-overview.md) · [Исследование №1](./rag-research-2026-10-27.md)

---

## 1. Принцип выбора

Сначала проверить **обязательные ограничения** (модели/egress, доступы, revision/citation correctness, обновление/удаление). Затем сравнить четыре основные цели с равным весом, не выдавая cache hit rate за отдельный показатель качества. Риски интеграции и стоимость — критерий выбора между близкими результатами. Не придумывать итоговый численный балл без измерений.

## 2. Вариант A: транзакционный hybrid RAG

~~~mermaid
flowchart TB
  T[Telegram] --> O[Orchestrator: auth, admission, exact cache]
  O --> H[Lexical and vector retrieval, fusion]
  H --> P[(PostgreSQL: revisions, ACL, chunks, pgvector, FTS)]
  H --> E[Evidence selection, optional rerank]
  E --> M[External grounded LLM]
  M --> V[Validate and authorize dispatch]
  V --> P
  V --> T
  U[Admin/source] --> I[Ingestion worker]
  I --> A[Immutable originals and staged revisions]
  A --> P
~~~

PostgreSQL — одновременно authority для source revisions, chunks, lexical/vector search, auth, jobs и generation pointer. Векторный поиск через pgvector (начать с exact на маленьком корпусе); лексический через ts_rank/ts_rank_cd и явную обработку technical identifiers. **PostgreSQL FTS не равен BM25.** Добавлять ANN/HNSW после проверки качества под ACL filters.

Параллелить подготовку query embedding и лексический поиск, если зависимости это допускают. Удалить ненужные chunks, передать модели короткие evidence с ID; один нормальный вызов генератора, затем citation validation и финальная проверка eligibility. Вариант экономит инфраструктурную интеграцию, но внешняя модель и filtered ANN могут ограничивать скорость.

При изменении документа source event фиксируется в durable state, старая версия может быть заблокирована до публикации новой. Работающий ingestion worker строит и проверяет chunks/embeddings, короткая SQL-транзакция публикует новый generation pointer. Stale worker не должен публиковать поверх более новой source event.

## 3. Вариант B: late interaction + локальная модель

~~~mermaid
flowchart LR
  U[Telegram] --> A[Orchestrator and scope]
  A --> Q[(Qdrant dense/sparse candidates)]
  Q --> X[Multivector MaxSim rescoring]
  X --> E[Grounded evidence]
  E --> L[vLLM local inference]
  L --> V[Answer and citation validation]
  S[Ingestion] --> Q
  P[(Control store and revisions)] --> A
~~~

Основные отличия: BGE-M3-подобные dense/sparse/multivector представления, MaxSim reranking и собственный inference engine. Может быть оправдан, если документы нельзя отправлять провайдеру, GPU действительно есть, а качество retrieval на терминах растёт.

Цена — GPU VRAM/host RAM, многовекторное хранение, токенные вычисления, эксплуатация vLLM и сложнее согласованность поискового индекса с authority. Это **не предпочтительный вариант при неизвестной GPU и квотах**. Указанные в исходном отчёте ресурсы — предварительные предположения, не измеренный minimum.

## 4. Вариант C: whole-document long-context / CAG

~~~mermaid
flowchart TB
  T[Telegram question] --> A[ACL and active generation]
  A --> F{Authorized corpus fits context?}
  F -->|yes| L[Stable document prefix plus question]
  L --> M[Long-context LLM]
  M --> V[Fine-grained source validation]
  F -->|no| R[Fallback retrieval architecture]
  R --> V
  V --> T
~~~

Если разрешённые документы помещаются в контекст, можно отказаться от chunk retrieval и переиспользовать документный prompt prefix на **новых** вопросах. Однако prefix cache уменьшает prefill, но **не** стоимость decode или всех входных токенов; холодные запросы, длинные документы, потеря фактов в середине, конфликтные сведения, ACL-фрагментация и frequent updates ухудшают вариант. Это не универсальный заменитель RAG.

## 5. Freshness, кеширование и четыре цели

**Строгая модель:** документная правка, известная приложению, ставит старую ревизию в pending/blocked. Ответ, рассчитанный на прежнем generation, при финальной проверке отбрасывается; разрешена одна повторная генерация, затем временный отказ. ACL/revoke/deletion проверяются перед отправкой. Граница гарантии — авторизация отправления, а не момент отображения Telegram на устройстве.

**Кеш:** exact ответ по вопросу, истории, scope, revision и настройкам. Одновременные идентичные запросы можно объединить. Semantic replay запрещён по умолчанию (отрицания, версии, роль пользователя). Shared prefix reuse допустим там, где он подтверждён backend/API и производительностью; retrieval-cache не помогает скорости самой LLM generation.

**Уникальные запросы:** минимизировать последовательные вызовы модели; сохранять признаки версий и идентификаторов при выборке; сжимать evidence без потери обязательных фактов; ограничивать output и token-aware queues; считать полезный goodput вместо RPS с отказами.

## 6. Предлагаемые SLO и бенчмарки

Эти значения — **цели эксперимента, не полученные результаты**.

| Сценарий | Цель в исследовании |
| --- | --- |
| Warm unique request, p95 backend | ≤ 8 с |
| Cold service unique request, p95 | ≤ 12 с (без первичной установки модели) |
| Exact answer hit, p95 backend | ≤ 0.5 с |
| Evidence Recall | ≥ 0.90 |
| Answer correctness | ≥ 0.85 |
| Citation entailment/completeness | ≥ 0.95 |

Проверять повторенные идентичные вопросы, unseen paraphrases, fully unique information needs, warm/cold states, 10 конкурентных клиентов, update/delete во время ответов, отрицания/версионность, unanswerable/conflict/injection, 429, нехватку quota и cache failure. Учитывать latency до финального backend результата отдельно от Telegram delivery; публиковать численность выборки, rejects и стоимость. Eval и holdout не использовать для подгонки одного решения под тест.

Исходный пакет включает 9 синтетических документов, 12 вопросов и 4 события изменений как **fixtures**. Исполняемого benchmark runner и измерений готовой системы нет.

## 7. Стратегия первых 48 часов

В начале подтвердить, что реально разрешено и доступно (корпус, GPU, LLM API, quota). В первые часы добиться одного сквозного grounded ответа и двух версий документа; затем тестировать hybrid/SQL exact, качество источников, кеш и нагрузку. При сильном успехе whole-document C на маленьком корпусе — позволить замены, **но не реализовывать сразу три архитектуры**.

Под конец — feature freeze и повторяемая демонстрация: неизвестный технический вопрос, десять разных уникальных вопросов, обновление и удаление источника, контроль cache hit/miss и отказ при недостатке доказательств. GraphRAG, сложный агентный конвейер, semantic answer cache, distributed KV и декоративные dashboards — ниже критического пути.

## 8. Вопросы, от которых зависит окончательный выбор

- Насколько большой и часто изменяемый корпус; какие таблицы, scan PDF, русский/английский?
- Нужны ли stringent send-time freshness и отзыв прав сразу после source event?
- Есть ли рабочая GPU и кто отвечает за локальный inference?
- Какие внешние API, лимиты и правила передачи документов?
- Из чего будет состоять hidden evaluation и какие веса четырёх целей?
- Допускаются ли заготовленные до хакатона инструменты и тестовые данные?

## Первоисточники

[pgvector](https://github.com/pgvector/pgvector) · [PostgreSQL isolation](https://www.postgresql.org/docs/18/transaction-iso.html) · [PostgreSQL text search](https://www.postgresql.org/docs/18/textsearch-controls.html) · [Qdrant multivectors](https://qdrant.tech/documentation/tutorials-search-engineering/using-multivector-representations/) · [vLLM prefix caching](https://docs.vllm.ai/en/stable/features/automatic_prefix_caching/) · [RAG vs long context](https://arxiv.org/abs/2407.16833) · [Cache-Augmented Generation](https://arxiv.org/html/2412.15605v2).

**Происхождение:** обзор по файлам executive brief, full research report, architecture alternatives, decision matrix, benchmark plan, execution plan и source register из INNOGLOBALHACK_RAG_Research.zip. Это читательский конспект; полные оригинальные статьи и многофайловый пакет в репозиторий здесь не копируются.
