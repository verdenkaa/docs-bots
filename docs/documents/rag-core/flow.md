---
title: Порядок обработки
sidebar_position: 4
---

:::warning Рабочий сценарий
Точки записи и ветвления заданы для MVP. Таймауты, предел попыток и задержки уточним при реализации.
:::

[Формат логов и замеры вызовов](../faq/logging.md) · [DTO и Pydantic](../faq/dto.md)

Внутри Python используются объекты DTO и доступ через атрибуты. При сборке `RagResult.sources` Core явно выбирает поля `SourceRef` из найденного `SearchFragment`: `SourceRef.model_validate(fragment.model_dump(include=set(SourceRef.model_fields)))`. Поле `text` в источник доставки не переносится; неизвестный `cited_chunk_id` остаётся ошибкой контракта. Exact Cache также явно собирает `RagResult` из `answer_text` и `sources`, не передавая служебное `corpus_generation` в строгую модель результата.

Совместимость этих переходов, JSON очередей, UUID, вложенных источников, пустого корпуса и веток отказа проверяется в `app/tests/test_contracts.py`. Проверка использует реальные сигнатуры и DTO с тестовыми ответами зависимостей; выполнение бизнес-логики, сроки и транзакции проверяются отдельно при реализации.

## Порядок вызовов и происхождение данных

| Шаг | Вызов и откуда берутся параметры | Действие по результату |
| --- | --- | --- |
| 0. Зарегистрировать вопрос | `handle_telegram_update(update)` извлекает из Telegram Update ID обновления, чат и текст. RAG API передаёт их в `register_question`. | Получает `request_id` и признак новой записи. Только для нового запроса появляется outbox `rag.questions`, Relay публикует его. [Вход RAG API](../telegram/module/entrypoints.md#handle-telegram-update) |
| 1. Взять вопрос | `claim_question(event.request_id)`. ID приходит из `rag.questions`. PostgreSQL отдаёт `raw_question`, закреплённое `corpus_generation` и `attempt`. | Если уведомление сейчас не требует действий, подтверждаем его. Если попытку взяли, проверяем её номер и запускаем обработку в пределах её срока. |
| 2. Допустить вопрос | `check_question(raw_question)`. Используем исходный текст из шага 1. | `allowed` даёт `query = normalized_question`. При `blocked` или `invalid_input` формируем просьбу изменить вопрос и переходим к шагу 6. При сбое детектора применяем политику повтора. |
| 3. Проверить Exact Cache | `get_answer(query, corpus_generation)`. Текст из Guardrails, поколение из шага 1. Exact Cache получает `CacheRead` из `read_cached_answer(query)`. | При совпадении поколения получаем `RagResult` для шага 6. При промахе или сбое Redis идём дальше. |
| 4. Найти фрагменты | `load_revision_ids(corpus_generation)` восстанавливает редакции из PostgreSQL. `search(query, revision_ids)` получает нормализованный вопрос и именно этот набор. | Searcher передаёт текст, `vectors[0]` из `Model Gateway.embed_texts(..., "query", ...)` и набор редакций в `find_chunks`. Возвращает `list[SearchFragment]`. Пустая выдача даёт сообщение о недостатке данных. |
| 5. Сформировать ответ | `generate_answer(query, fragments)`. Текст вопроса из шага 2, упорядоченные фрагменты из шага 4. | При `answered` собираем `RagResult` из текста генератора и метаданных фрагментов по `cited_chunk_ids`. Неизвестный ID считаем технической ошибкой контракта. При `insufficient_evidence` формируем сообщение о недостатке данных. |
| 6. Зафиксировать итог | `complete_question(attempt, result)`. Попытка из шага 1, итог из соответствующей ветки. | Runtime сохраняет результат, снимает закрепление и создаёт outbox для доставки. При успешном коммите для нового ответа по документам вызываем `store_answer(query, corpus_generation, result)`. Затем подтверждаем уведомление. |
| 7. Передать в доставку | Relay передаёт запись outbox в `publish_notice`. RabbitMQ вызывает `handle_rag_result(event)` через адаптер. `claim_delivery` отдаёт активную попытку, `chat_id` и сохранённый `RagResult`. | `send_answer(chat_id, result)` возвращает исход Telegram. RAG API сохраняет его или планирует повтор по взятой попытке. |

~~~mermaid
flowchart TD
    IN["rag.questions → взять вопрос и закрепить поколение"] --> G["Guardrails → нормализованный вопрос"]
    G -->|"допуск"| C["Exact Cache"]
    G -->|"отказ или некорректный ввод"| SAVE
    C -->|"попадание"| SAVE
    C -->|"промах"| S["Получить ID редакций → Searcher"]
    S -->|"есть фрагменты"| A["Answer Generator → текст и ID чанков"]
    S -->|"пусто"| SAVE
    A --> SAVE["Сохранить итог, снять закрепление и записать outbox"]
    SAVE --> OUT["rag.results → RAG API"]
~~~

## Внутренние вызовы моделей и происхождение параметров {#model-calls}

Точки входа Core и бизнес-модулей не меняются. Model Gateway вызывают сами модули; Core получает прежние `GuardrailsResult`, `list[SearchFragment]` и `GenerationResult`. Функции и поля: [контракт Model Gateway](../model-gateway/entrypoints.md).

| Этап и вызов | Откуда доступны параметры | Как используется результат |
| --- | --- | --- |
| Guardrails → `classify_injection(raw_question, timeout_s=detector_timeout_s)` | `raw_question` получен Core из `claim_question` и передан в `check_question`. Лимит шага — конфигурация Runtime. | `InjectionResult`: сверяем `profile_id`, применяем правило класса/score и порога из конфигурации Guardrails. При допуске нормализуем текст, при `StepError` возвращаем `detector_unavailable`. |
| Searcher → `embed_texts([query], "query", timeout_s=embedding_timeout_s)` | `query` — результат Guardrails, полученный через `search`. Лимит и ожидаемый профиль — конфигурация Runtime. | Сверяем `EmbeddingResult.profile_id`, передаём `vectors[0]` в `find_chunks` вместе с `query`, `revision_ids` из Core и настроенным `candidate_limit`. |
| Searcher → `rerank(query, candidates, timeout_s=rerank_timeout_s)` | `candidates = [hit.fragment.text for hit in hits]` из Qdrant. Вызов только при включённом reranker и непустых hits. | Оценки связываем по `candidate_index` с исходными hits, сортируем и отбираем. Все ID и метаданные берём из исходных fragment. Отключённый шаг сохраняет порядок Qdrant. |
| Answer Generator → `generate(messages, options, timeout_s=generation_timeout_s)` | `query` и `fragments` приходят от Core. Из них генератор собирает messages и соответствие S1 → chunk_id. Options и лимит — конфигурация Runtime. | Только `completed`: разбираем text как бизнес-JSON, проверяем citations и преобразуем в `GenerationResult`. Ошибки Gateway пробрасываем как `StepError`, `length`/`filtered` дают `generation_incomplete`. |
| Ingestion Worker → `embed_texts(texts, "document", timeout_s=embedding_timeout_s)` | `texts` — результат `prepare_chunks`, бюджет ограничен сроком взятого задания. ID документа/редакции уже доступны из задания. | Проверяем профиль и число vectors; `vectors[chunk_no]` вместе с текстом/метаданными идёт в `ChunkPoint.dense`, затем в `write_revision_points`. |

Core ограничивает всю обработку сроком `attempt.deadline_at`; фиксированные лимиты внутренних шагов не продлевают попытку. Gateway ограничивает собственный вызов, включая ожидание слота. Отмена ожидания не гарантирует прекращения вычисления: правила локальных исполнителей и удалённых серверов описаны в [жизненном цикле](../model-gateway/lifecycle.md). Повторы планируют Core/Worker, а не Gateway.

**Проверка коротких путей.** Пустой/заблокированный ввод не вызывает embedding и генерацию. Попадание Exact Cache пропускает поиск и генерацию, но не Guardrails. Пустой `revision_ids` не вызывает модели поиска; пустая выдача не вызывает reranker и генерацию. Для Core не требуются отсутствующие у него model_id, ключи API или тексты чанков до поиска: их предоставляет соответствующий владелец.

## Транзакции и повторы

- **Взятие вопроса.** Под короткой блокировкой по чату `claim_question` проверяет порядок по `request_id`, готовность по `state` и `resume_at`, затем переводит строку в `processing`, увеличивает `attempt_no` и записывает срок попытки. При первом взятии под блокировкой `corpus_state` сохраняет `pinned_generation`. При повторе использует сохранённое поколение. Список редакций можно прочитать позже, поскольку закрепление уже защищает его от очистки. [Видимость редакций](../architecture/data-and-versions.md#как-меняется-версия-корпуса)
- **Ожидание и дубликаты.** `claim_question` возвращает `None` для завершённой обработки, активной попытки, слишком раннего уведомления или запроса, который ждёт предыдущий в чате. Если строки нет, адаптер PostgreSQL фиксирует диагностическое событие и тоже возвращает `None`. В обоих случаях Core подтверждает уведомление. При завершении предыдущего вопроса Runtime будит следующий готовый через outbox под той же блокировкой чата.
- **Прикладной повтор.** При временном сбое Guardrails, Searcher или Answer Generator и наличии попыток Core вычисляет `retry_at` и вызывает `schedule_question_retry(attempt, retry_at)`. Runtime переводит строку в `queued`, сохраняет время и уведомление `rag.questions` с тем же `available_at`. Закрепление поколения сохраняется. При восстановлении снова выполняем шаги начиная с Guardrails по тому же вопросу и поколению.
- **Окончание обработки.** При постоянном сбое или исчерпании попыток Core формирует техническое сообщение и сохраняет его обычным путём. Если последняя разрешённая попытка прервалась, восстановление берёт новую попытку с номером выше лимита только для записи этого итога. Шаги поиска и генерации в ней пропускаются.
- **Коммит и ACK.** `complete_question` одной транзакцией пишет `result`, очищает `pinned_generation`, переводит строку в `ready_to_deliver`, сбрасывает поля попытки для доставки и создаёт запись `rag.results`. Оба перехода, завершение и повтор, сверяют состояние `processing`, номер и действующий срок попытки. При утрате права на запись Core подтверждает уведомление без записи в кеш. Если PostgreSQL недоступен или итог транзакции неизвестен, Core возвращает `retry_broker`. Повторное чтение установит, был ли переход сохранён.

Вызовы детектора, Redis, Qdrant и модели идут вне транзакций PostgreSQL. Если процесс упал между взятием и коммитом, восстановление находит попытку после истечения срока. Если результат уже записан, повтор уведомления не запускает обработку ещё раз. [Восстановление после рестарта](../architecture/lifecycle.md#кто-продолжает-работу-после-рестарта)

## Проверка стыков

| Сценарий | Какие данные доступны и куда идём |
| --- | --- |
| В кеше есть ответ, Guardrails блокирует вопрос | Guardrails получил исходный текст. `normalized_question` отсутствует, поэтому обращения к кешу и поиску пропускаются. Для фиксации отказа уже есть `attempt`. |
| Поколение 41 закреплено, пока Worker публикует 42 | Сравниваем кеш с 41. При промахе восстанавливаем ID редакций для 41. Если Redis уже хранит 42, пропускаем его запись без удаления. |
| Searcher ничего не нашёл | Генератору требуются фрагменты. Core сразу сохраняет текст о недостатке данных с пустым списком источников. |
| Генератор указал только второй чанк из выдачи | Core копирует метаданные второго чанка в `RagResult.sources`. Для доставки хватает сохранённого итога, даже если старые точки Qdrant уже очищены. |
| Процесс упал после записи результата | В PostgreSQL уже есть итог и запись outbox. Повтор подтверждает уведомление без новой генерации. |

[Общие DTO](./contracts.md) · [Хранение и блокировки](../infrastructure/postgresql/index.md#вопросы-и-доставка)

## Стык с Ingestion

Ingestion публикует редакции в отдельном потоке. RAG Core получает результат публикации через PostgreSQL и Qdrant.

| Переход | Какие данные реально переходят дальше |
| --- | --- |
| `accept_upload` → `handle_ingestion_job` | Gateway получает `staging_ref` из тома. PostgreSQL сохраняет её рядом с `revision_id`, а в RabbitMQ уходит только `JobRef.job_id` с тем же UUID. Worker получает ссылку вызовом `claim_ingestion_job`. |
| `open_staged_upload` → `write_revision_points` | Том отдаёт путь и манифест. Worker получает тексты и локаторы из парсера, векторы — из `EmbeddingResult.vectors` от Model Gateway. Добавляет `document_id` из задания, `revision_id` из `job_id` и метаданные из манифеста. |
| `begin_revision_write` → `write_revision_points` → `publish_revision` | Worker сначала сохраняет ожидаемое число чанков и признак незавершённой записи в PostgreSQL. После подтверждения полного набора точек публикация снимает признак, открывает интервал новой редакции и, например, возвращает поколение 42. |
| `claim_question` → `load_revision_ids` → `search` | Новый запрос получает поколение 42 и на промахе кеша ищет по его редакциям. Уже начатый запрос с поколением 41 ищет по редакциям 41. В обоих случаях у Searcher есть разрешённые UUID, а тексты и источники он получает через `find_chunks`. |

[Полный контракт Ingestion](../ingestion/contracts.md#цепочка-загрузки-и-публикации)
