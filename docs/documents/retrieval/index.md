---
title: Searcher
sidebar_position: 1
---

**[Точка входа Searcher](./entrypoints.md#search)** — сигнатуры, параметры, форматы результата и примеры.

## Граница

Searcher находит фрагменты, которые помогут ответить на вопрос. RAG Core вызывает его внутри процесса `core` после Guardrails и промаха Exact Cache. Searcher получает эмбеддинги и оценки reranker через [Model Gateway](../model-gateway/index.md), читает Qdrant через адаптер Runtime, а запись фрагментов и публикация редакций принадлежат Ingestion Worker. Core передаёт найденные фрагменты в Answer Generator, который собирает контекст и формирует ответ. [Сборка модулей](../architecture/module-composition.md)

## Дальше

- [Конвейер поиска](./pipeline.md) описывает dense, BM25, RRF и подключение reranker.
- [Проверка качества](./evaluation.md) фиксирует, что есть в прототипе Ромы и как выбрать настройки для кейса.
