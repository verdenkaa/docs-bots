---
title: RAG Core
sidebar_position: 1
---

**[Точка входа RAG Core](./entrypoints.md#handle-rag-question)** — сигнатуры, параметры, форматы результата и примеры.

## Граница

RAG Core обрабатывает один вопрос без истории диалога. Он выбирает следующие шаги, ограничивает параллельную обработку и сохраняет цельный результат перед передачей в доставку. RAG API регистрирует вопросы и доставляет ответы. Ingestion Worker публикует редакции, по которым работает поиск. [Место в архитектуре](../architecture/module-composition.md)

## Вызовы по пути запроса

| Точка входа | Назначение |
| --- | --- |
| <span className="operation-badge operation-badge--db">DB</span> [`claim_question`](../infrastructure/postgresql/rag-core.md#claim-question) | Runtime берёт единственный активный вопрос сотрудника и закрепляет поколение. |
| <span className="operation-badge operation-badge--async">ASYNC</span> [`recognize_text`](../model-gateway/entrypoints.md#recognize-text) | Для изображения — текст OCR; Core сохраняет подпись/OCR до Guardrails. |
| <span className="operation-badge operation-badge--db">DB</span> [`load_revision_ids`](../infrastructure/postgresql/rag-core.md#load-revision-ids) | Набор закреплённого поколения до проверки темы и кеша. |
| <span className="operation-badge operation-badge--async">ASYNC</span> [`check_question`](../guardrails/entrypoints.md#check-question) | Guardrails получает текст/OCR и revision_ids, проверяет тему и нормализует. |
| <span className="operation-badge operation-badge--cache">CACHE</span> [`get_answer`](./exact-cache-entrypoints.md#get-answer) | Exact Cache ищет готовый ответ для вопроса и закреплённого поколения. |
| <span className="operation-badge operation-badge--async">ASYNC</span> [`search`](../retrieval/entrypoints.md#search) | Searcher находит фрагменты по нормализованному вопросу и закреплённым редакциям. |
| <span className="operation-badge operation-badge--async">ASYNC</span> [`generate_answer`](./answer-generator-entrypoints.md#generate-answer) | Генератор возвращает текст, ID чанков и изображений либо недостаток подтверждений. |
| <span className="operation-badge operation-badge--db">DB</span> [`complete_question`](../infrastructure/postgresql/rag-core.md#complete-question) | Runtime сохраняет QuestionOutcome, освобождает редакции и создаёт доставку либо case Support. |
| <span className="operation-badge operation-badge--sub">SUB</span> [`handle_rag_result`](../telegram/module/entrypoints.md#handle-rag-result) | RAG API получает DeliveryRef и продолжает сохранённую доставку. |

[Общие DTO](./contracts.md) · [Порядок обработки](./flow.md) · [Значения тегов](../faq/index.md#теги-контрактов)
