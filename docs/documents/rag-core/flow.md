---
title: "Порядок обработки"
sidebar_position: 4
toc_max_heading_level: 2
---

## Порядок вызовов и происхождение данных

| Шаг | Вызов и доступные параметры | Результат / следующий путь |
| --- | --- | --- |
| 0. Приём | RAG API читает from.id, chat, текст/photo/caption и время. `register_question(NewQuestion(...))` проверяет допуск, повтор и слот. | accepted даёт request_id и outbox RequestRef. Управление Support, busy и denied не создают вопрос. |
| 1. Попытка | `claim_question(event.request_id)` из rag.questions. | ClaimedQuestion: input, prepared_text, received_at, attempt и закреплённое поколение. None → ACK. |
| 2. Текст | TextQuestion.text используется напрямую. ImageQuestion при prepared_text=None → `get_question_image(file_id)` → `recognize_text(image, timeout_s=...)`. | Подпись + OCR дают raw_question; непустой результат сохраняется save_prepared_question до проверки. Пустой текст без подписи → NoticeOutcome(no_text). На повторе prepared_text уже доступен. |
| 3. Допуск и тема | `load_revision_ids(corpus_generation)` до Guardrails. `check_question(raw_question, revision_ids)`. | allowed даёт query. blocked/invalid_input → NoticeOutcome. out_of_scope → SupportOutcome(out_of_scope). Если редакций нет, после базовых проверок → SupportOutcome(no_evidence), без Qdrant/генерации. |
| 4. Кеш | `get_answer(query, corpus_generation)`. | RagResult с Markdown, sources и images → AnsweredOutcome. Кеш не обходит Guardrails. Промах/сбой Redis → поиск. |
| 5. Поиск | `search(query, revision_ids)` с тем же набором из шага 3. | Фрагменты уже содержат text, SourceRef и images. Пусто → SupportOutcome(no_evidence). |
| 6. Генерация | `generate_answer(query, fragments)`. | answered даёт answer_text, cited_chunk_ids и cited_image_ids. Core выбирает только известные источники/изображения. insufficient_evidence → SupportOutcome(no_evidence). |
| 7. Решение | `complete_question(attempt, outcome)`. | Атомарно сохраняет исход, освобождает поколение и создаёт доставку либо case Support. При новом успешном ответе `store_answer(query,generation,result)` после коммита. |
| 8. Поддержка | `run_support_batch` подбирает creating-case, вызывает create_support_ticket. | Открытый тикет, снимок исходного вопроса и ticket_link-доставка с DeliveryRef. Слот до терминальной доставки, не до закрытия тикета. |
| 9. Доставка | `handle_rag_result(DeliveryRef)` → claim_delivery. | Собрать/сохранить части, отправить с next_part, сохранить квитанции и завершить/повторить доставку. Core не запускается повторно. |

~~~mermaid
flowchart TD
 I["RequestRef → input, попытка, поколение"] --> T["Текст или сохранённый OCR"]
 T --> G["ID редакций → Guardrails"]
 G -->|"допуск"| K["Exact Cache"]
 G -->|"вне темы"| H["SupportOutcome"]
 G -->|"отказ"| N["NoticeOutcome"]
 K -->|"попадание"| A["AnsweredOutcome"]
 K -->|"промах"| S["Searcher"]
 S -->|"фрагменты"| L["Answer Generator"]
 S -->|"пусто"| H
 L -->|"ответ"| A
 L -->|"нет подтверждений"| H
 A --> DB["complete_question: сохранить исход"]
 H --> DB
 N --> DB
 DB --> D["DeliveryRef или case Support"]
~~~

## Внутренние вызовы моделей и происхождение параметров {#model-calls}

| Владелец / вызов | Что передаёт | Что проверяет |
| --- | --- | --- |
| Core → recognize_text | ImageContent от Runtime, лимит из остатка попытки. | OcrResult.text; пустой OCR не является техническим сбоем. Подпись сохраняет контекст, OCR не исправляет URL/операторы произвольно. |
| Guardrails → classify_injection | raw_question из текста/OCR. | Профиль и правило класса/порога. Бессмыслицу проверяет локальный простой классификатор Guardrails. |
| Guardrails → embed_texts → find_topic_matches | Нормализованный query, purpose=query, затем вектор и revision_ids. | Профиль примеров и косинусное сходство. Недоступный/неполный индекс → техническая ошибка, не «вне темы». |
| Searcher → embed_texts / rerank | query; тексты кандидатов только после поиска. | Профиль чанков, индексы кандидатов. Переиспользование вектора — будущая оптимизация, скрытого обмена нет. |
| Answer Generator → generate | query, тексты, локальные метки источников и допустимые ImageRef. | completed, бизнес-JSON и все цитируемые ID. |
| Worker → embed_texts / generate | Подготовленные чанки и примеры вопросов. | Для чанков purpose=document; для примеров вопросов purpose=query, как у проверки темы. |

Все лимиты вызовов укладываются в технический срок попытки. Он задаётся отдельно от целевых 20 секунд. Отмена ожидания не гарантирует остановку модели; применяется [протокол исполнителей](../model-gateway/lifecycle.md).

## Транзакции и повторы

- При claim поколение закрепляется под синхронизацией публикации/очистки. Внешние вызовы выполняются вне транзакции.
- OCR-текст сохраняется по действующей попытке; повтор использует его, исходное received_at и то же поколение.
- При временном StepError Core планирует schedule_question_retry. Постоянный сбой/исчерпание попыток дают NoticeOutcome(technical_error); низкая релевантность с техническим сбоем не смешивается.
- complete_question сверяет номер/срок и сохраняет только один QuestionOutcome. Для answer/notice создаётся одна самостоятельная терминальная доставка; для support — один case. Утрата права даёт False: не пишем кеш и прекращаем работу.
- При неизвестном коммите StorageError даёт retry_broker. Повтор сначала читает состояние. Неизвестная публикация Relay не запускает новую генерацию.
- Запись кеша только после подтверждённого answered-коммита. Более старое поколение не перезаписывает новое.

## Проверка стыков

| Путь | Проверка доступности параметров |
| --- | --- |
| Текст → ответ | input.text уже сохранён; query появляется только после allowed; тексты/изображения для генератора — только после поиска. |
| Изображение → ответ | file_id доступен до OCR; prepared_text появляется до Guardrails. Кеш хранит готовый ответ, не байты пользовательского изображения. |
| Кеш | Guardrails и актуальный набор примеров проверены; источники/изображения уже есть в RagResult, дополнительных чанков для доставки не нужно. |
| Вне темы / нет подтверждений | Причина типизирована, request_id/владелец уже в PostgreSQL; Support не получает выдуманный answer_text. |
| Инъекция / бессмыслица | normalized_question может отсутствовать; кеш, поиск и генерация пропускаются. |
| Ответ с картинкой | cited_image_ids входят в images найденных фрагментов, плейсхолдеры совпадают. В доставке UUID разрешает Runtime, а не LLM. |
| Рестарт после части ответа | delivery_id, неизменяемый план, next_part и квитанции сохранены. Новая попытка не меняет request_id/результат. |
| Кнопка поддержки | Callback указывает свой request_id/case_id; update и действие дедуплицируются отдельно от Core. |
| Медленный ответ | После 10 секунд статус, после 20 только превышение метрики. Действующая техническая попытка может ответить позже. |
| Публикация нового поколения | Тема и поиск текущего вопроса используют старые revision_ids; новые вопросы берут новое поколение. |

## Стык с Ingestion

Gateway передаёт Worker готовый Markdown, плейсхолдеры и ImageRef; source_name — название исходного документа. Изображения уже записаны Gateway в S3, оригинал не сохранён.

Worker сохраняет images рядом с каждым PreparedChunk/ChunkPoint. Qdrant возвращает их в SearchFragment без генерации новых ID. Чанки и примеры одной редакции публикуются совместно через PostgreSQL. SourceRef сохраняет document_id и source_name; RAG API строит ссылку на HTML-заглушку документа, локатор раздела необязателен.

[Общие DTO](./contracts.md) · [API и доставка](../telegram/module/entrypoints.md) · [PostgreSQL Core](../infrastructure/postgresql/rag-core.md)

До первого вопроса Ingestion подтверждает S3 и Markdown, Worker закрепляет PreparedRevision и публикует только два полных набора. Guardrails использует topic_questions, Searcher — document_chunks; ни один модуль не должен самостоятельно выбирать другое поколение. Searcher передаёт lexical_priority в Runtime по наличию ссылки; вектор вопроса пока вычисляется независимо от Guardrails.

Отключение сотрудника после приёма не добавляет новый отказ в пайплайн: claim/повторы/Core продолжаются, результат или ссылка автоматической эскалации доставляются. Допуск на новый вопрос проверяет RAG API при следующей регистрации.
