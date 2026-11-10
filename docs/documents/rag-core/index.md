---
title: RAG Core
sidebar_position: 1
---

:::warning Рабочая спецификация
Сигнатуры и DTO предложены для реализации MVP. Провайдер генерации, параметры повторов и формат ссылок в Telegram уточним по кейсу.
:::

**[Точка входа RAG Core](./entrypoints.md#handle-rag-question)** — сигнатуры, параметры, форматы результата и примеры.

## Граница

RAG Core обрабатывает один вопрос без истории диалога. Он выбирает следующие шаги, ограничивает параллельную обработку и сохраняет цельный результат перед передачей в доставку. RAG API регистрирует вопросы и доставляет ответы. Ingestion Worker публикует редакции, по которым работает поиск. [Место в архитектуре](../architecture/module-composition.md)

## Вызовы по пути запроса

| Точка входа | Назначение |
| --- | --- |
| <span className="operation-badge operation-badge--db">DB</span> [`claim_question`](../infrastructure/postgresql/rag-core.md#claim-question) | Runtime берёт следующий вопрос чата и закрепляет поколение. |
| <span className="operation-badge operation-badge--async">ASYNC</span> [`check_question`](../guardrails/entrypoints.md#check-question) | Guardrails проверяет исходный текст и при допуске возвращает нормализованный вопрос. |
| <span className="operation-badge operation-badge--cache">CACHE</span> [`get_answer`](./exact-cache-entrypoints.md#get-answer) | Exact Cache ищет готовый ответ для вопроса и закреплённого поколения. |
| <span className="operation-badge operation-badge--async">ASYNC</span> [`search`](../retrieval/entrypoints.md#search) | Searcher находит фрагменты по нормализованному вопросу и закреплённым редакциям. |
| <span className="operation-badge operation-badge--async">ASYNC</span> [`generate_answer`](./answer-generator-entrypoints.md#generate-answer) | Answer Generator формирует текст и ID использованных чанков либо сообщает о нехватке данных. |
| <span className="operation-badge operation-badge--db">DB</span> [`complete_question`](../infrastructure/postgresql/rag-core.md#complete-question) | Runtime сохраняет итог, освобождает редакции и ставит уведомление для RAG API. |
| <span className="operation-badge operation-badge--sub">SUB</span> [`handle_rag_result`](../telegram/module/entrypoints.md#handle-rag-result) | RAG API получает `rag.results` и доставляет сохранённый ответ. |

[Общие DTO](./contracts.md) · [Порядок обработки](./flow.md) · [Значения тегов](../faq/index.md#теги-контрактов)
