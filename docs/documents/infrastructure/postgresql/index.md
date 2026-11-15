---
title: "PostgreSQL"
sidebar_position: 1
toc_max_heading_level: 2
---

## Что вызывать

| Задача | Контракт |
| --- | --- |
| Приём, ledger update, слот и доставка | [RAG API](./rag-api.md) |
| Вопрос, OCR-текст, поколение и бизнес-итог | [RAG Core](./rag-core.md) |
| Допуски, ожидания, тикеты, реплики, метрики | [Access/Support](./support.md) |
| Загрузка, индекс, публикация | [Ingestion](./ingestion.md) |
| Очистка временных/поисковых данных | [Очистка](./cleanup.md) |
| Relay и восстановление | [Outbox](./outbox.md) |
| HTML-заглушка источника | [load_source_metadata](../media.md#load-source-metadata) |
| Защита повторного inference и сверка неизвестных исходов | [Журнал модельных вызовов](../runtime-entrypoints.md#журнал-модельных-вызовов) |

Один PostgreSQL с именованным томом. Оба контейнера используют Runtime; бизнес-правила принадлежат владельцам. Транзакции короткие, внешние вызовы Telegram/S3/Qdrant/моделей идут вне них.

## Слой хранения

Таблицы представлены внутренними ORM-моделями SQLAlchemy в app/rag/runtime/postgresql/_models.py. Публичные DTO от них не наследуются и не содержат Session. Каждый вызов репозитория получает собственную AsyncSession и транзакцию; сессия не разделяется между параллельными задачами. Для чтения используются select по моделям, для записи — ORM-объекты и ORM-enabled insert/update. JSON передаётся как типизированные значения, а не как SQL-строки с ручными CAST.

Блокировки строк задаются with_for_update, восстановление и очистка — выражениями exists/union и skip_locked. Специфичные PostgreSQL advisory lock и clock_timestamp изолированы в инфраструктурных функциях и также вызываются через SQLAlchemy func. Повторное чтение после ожидания блокировки обновляет ORM-состояние из БД, чтобы identity map не вернула устаревшую запись.

Схему меняет только Alembic: create_all при импорте или запуске приложения не вызывается. Миграции генерируются из полной ORM-метадаты командой `alembic revision --autogenerate`; `alembic check` проверяет отсутствие расхождений. Начальная ревизия — `001_initial`. Подготовка и независимая проверка тестовых данных могут использовать SQL внутри тестов. Рабочие репозитории не принимают строковые запросы. [Команды и обновление схемы](../runtime-entrypoints.md).

## Таблицы {#четыре-таблицы}

Сохраняем таблицы состояний и добавляем documents для уникального имени и указателя на опубликованную версию. Всего десять таблиц. Отдельных таблиц для каждого DTO, изображения ответа или фонового таймера нет.

| Таблица | Зачем отдельно |
| --- | --- |
| rag_requests | Вопрос и его единственный бизнес-итог. |
| documents | Уникальное имя, стабильный document_id и текущая опубликованная версия. |
| document_revisions | Редакция, подготовленный Markdown, публикация и очистка. |
| corpus_state | Одна строка номера поколения корпуса. |
| outbox | Надёжное намерение уведомления брокеру. |
| employees | Список разрешённых Telegram username. |
| telegram_updates | Дедупликация в том числе busy/denied/кнопок, для которых вопрос не создаётся. |
| rag_deliveries | Несколько самостоятельных отправок: статус, ответ, фото, поддержка; их отдельные попытки. |
| support_cases | Ожидание и сам тикет в разных состояниях, снимок контекста. |
| support_messages | Разговор и независимая дедупликация реплик. |

Техническая таблица model_calls хранит хеш полного модельного входа с пространством request/job, токен и PID/host владельца, профиль/роль/операцию, состояние executing/completed/uncertain, проверенный результат JSONB и timestamps. Она не добавляет бизнес-сущность; Gateway получает координатор от Runtime и не обращается к БД. Результаты не истекают автоматически, неизвестные исходы требуют сверки. Alembic отдельно хранит версию схемы в alembic_version.

~~~mermaid
erDiagram
 RAG_REQUESTS ||--o{ RAG_DELIVERIES : "доставки"
 RAG_REQUESTS ||--o| SUPPORT_CASES : "обращение"
 SUPPORT_CASES ||--o{ SUPPORT_MESSAGES : "реплики"
 RAG_REQUESTS o|--o{ OUTBOX : "вопрос"
 RAG_DELIVERIES o|--o{ OUTBOX : "доставка"
 DOCUMENT_REVISIONS o|--o{ OUTBOX : "индексация"
~~~

### rag_requests {#rag_requests}

| Поле | Тип | Смысл |
| --- | --- | --- |
| request_id | bigint PK | ID вопроса, не ID доставки. |
| user_id, chat_id | bigint | Стабильный владелец и личный чат. |
| username | text | Снимок разрешённого имени на момент приёма, не FK на изменяемый список. |
| update_id | bigint UNIQUE | Update, создавший вопрос. |
| input | jsonb | QuestionInput: text либо file_id/caption. |
| received_at | timestamptz | Первый приём, неизменен. |
| prepared_text | text NULL | Сохранённый OCR-текст с подписью. |
| state | text | queued, processing, awaiting_delivery, awaiting_support, finished. |
| pinned_generation | bigint NULL | Закрепление только поиска/проверки темы. |
| result | jsonb NULL | QuestionOutcome; answered содержит RagResult, support — причину. |
| attempt_no, resume_at | integer, timestamptz NULL | Попытка обработки и её срок/повтор. Доставка имеет свои поля в другой таблице. |

Уникальный активный вопрос по user_id для state != finished. Окончательный исход доставки освобождает слот, готовность текста сама по себе — нет.

### rag_deliveries {#rag-deliveries}

| Поле | Тип | Смысл |
| --- | --- | --- |
| delivery_id | bigint PK | Ссылка DeliveryRef в rag.results, порядок в чате. |
| user_id, chat_id | bigint | Получатель и назначение. |
| request_id, case_id | bigint NULL, uuid NULL | Связи, если применимы. |
| purpose | text | answer, notice, status, busy, denied, first_question, support_offer, support_confirmation, ticket_link. |
| content | jsonb NULL | RagResult для answer/notice; остальные сообщения строит API по purpose/связанному case. |
| terminal_for_request | boolean | Конечная доставка вопроса, освобождающая слот. Статусы/ручное ожидание им не являются. |
| parts, receipts | jsonb NULL, jsonb | Неизменяемый список DeliveryPart и подтверждения по индексу с message_id/variant/confirmed_at (UTC-время фиксации подтверждения). |
| next_part | integer | Первая неподтверждённая часть. |
| state | text | waiting, sending, delivered, failed, unknown, cancelled. |
| attempt_no, resume_at, delivery_uncertain | integer, timestamptz NULL, boolean | Попытки доставки, время/срок и накопленная неоднозначность. |

Уникальность status на request_id; одна terminal_for_request-доставка вопроса. Другие уведомления имеют ключи исходного update/case/стадии, не создаются повторно.

### employees {#employees}

username text PK (нижний регистр, без @), enabled boolean. Правило доступа относится к текущему имени. Активный слот и владение вопросом по-прежнему используют Telegram user_id. Статичные ADMIN_LOGIN/ADMIN_PASSWORD не хранятся в employees.

### telegram_updates {#telegram-updates}

update_id bigint PK, user_id/chat_id bigint NULL, received_at timestamptz, action jsonb NULL, result jsonb. result содержит надёжный IntakeResult и ссылки. action хранит SupportCommand: нормализованное действие, username, origin и callback_message_id для привязки причины offer; секретов/всего Telegram payload не требуется. У accepted вопрос есть, у busy/denied — нет. retry_later не является окончательным сохранённым исходом.

### support_cases {#support-cases}

case_id uuid PK (случайный UUIDv4), request_id bigint UNIQUE, user_id bigint, state text, reason text, check_at timestamptz NULL, ticket_url text NULL, created_at/closed_at timestamptz NULL, history jsonb. Состояния — из SupportCase. Waiting ещё не созданный тикет, open/closed — уже созданный. history — снимок одного вопроса и реального ответа, не весь диалог. Нажатие не сдвигает уже начатый check_at.

### support_messages {#support-messages}

message_id bigint PK, case_id uuid FK, author_role text (visitor/operator), author_login text NULL, client_message_id uuid, text text, created_at timestamptz. author_login задан только у оператора и получен из проверенного Principal. Уникальный ключ (case_id,author_role,client_message_id) отделяет реплики посетителей от оператора. Посетители анонимны; у них нет author_id. Повтор с тем же текстом возвращает прежнюю реплику даже после закрытия тикета.

### documents {#documents}

document_id uuid PK, source_name text UNIQUE с точным регистрозависимым сравнением, current_revision_id uuid NULL, current_version integer NULL. Оба current-поля отсутствуют до первой публикации и переключаются вместе. Первая версия — 0. Имя неизменно; переименование создаёт другой документ. Регистрация по имени и резервирование версии сериализуются по этому документу. Метка доступа относится к редакции и не подменяет проверку отправителя загрузки.

### document_revisions {#document_revisions}


| Поле | Тип | Что хранит и зачем |
| --- | --- | --- |
| revision_id | uuid PK | ID редакции, равен job_id; не меняется при повторе. |
| document_id | uuid | Идентичность документа между заменами; FK на documents. |
| revision_no | integer | Порядок регистрации загрузок внутри document_id; уникальная пара, не номер опубликованной версии. |
| document_version, base_version | integer NULL | Зарезервированный номер публикации с 0 и предыдущая опубликованная версия. До резервации оба NULL; первая версия 0 имеет базу NULL. |
| access_level | text | Обязательная метка документа из DocumentBatch.source, сохраняется в точках. |
| source_name | text | Название исходника для чанков и HTML-заглушки, сохраняется после уборки точек. |
| image_ids | jsonb | Список UUID изображений S3 этой редакции; без байтов/секретов. |
| staging_ref | text NULL | Ключ готового Markdown, манифеста и PreparedRevision; очищается после удаления каталога. |
| state | text | queued, indexing, published, retired, failed, deleting или cleaned. |
| attempt_no | integer | Число взятий задания; новая попытка увеличивает его. |
| resume_at | timestamptz NULL | В queued — время готовности; в indexing — deadline текущей попытки. |
| qdrant_write_pending | boolean | Запись любого из двух наборов начата; при неизвестном исходе блокирует повтор/очистку. |
| expected_chunk_count | integer NULL | Ожидаемый полный набор UUIDv5 чанков; положителен после begin_revision_write. |
| expected_topic_count | integer NULL | Ожидаемый полный набор UUIDv5 примеров темы; положителен после begin_revision_write. |
| visible_from | bigint NULL | Поколение публикации включительно; до публикации NULL. |
| visible_until | bigint NULL | Поколение замены исключительно; NULL — открытая верхняя граница. |

staging_ref не указывает на оригинал. Оба ожидаемых числа сохраняются после публикации; при очистке сохраняются также идентичность, название, image_ids и интервалы. Наличие одного revision_id в Qdrant не подтверждает полноту редакции.

### corpus_state {#corpus_state}

singleton_id smallint PK — постоянный ключ единственной строки; generation bigint — номер опубликованного состава. Пустой корпус имеет 0. Это версия всего набора, включая примеры темы, не ID одного документа.

### outbox {#outbox}

message_id uuid PK, route text, request_id bigint NULL, delivery_id bigint NULL, revision_id uuid NULL, available_at/published_at timestamptz. Ровно одна ссылка соответствует маршруту: rag.questions → request_id, rag.results → delivery_id, ingestion.jobs → revision_id. AMQP message_id — ID этой записи, не ID работы.

## Вопросы и доставка {#вопросы-и-доставка}

Блокировка допуска применяется по username (включая отсутствующую запись), затем блокировка/уникальность активного слота — по user_id. Транзакция приёма включает ledger, вопрос и outbox либо ledger с уведомлением отказа. Попытки Core не смешиваются с DeliveryAttempt.

complete_question освобождает pinned_generation и сохраняет исход: answered/notice → awaiting_delivery, support → awaiting_support. Support после открытия тикета создаёт терминальную ticket_link-доставку. complete_delivery переводит исходный вопрос в finished только для terminal_for_request. Очереди следующего вопроса сотрудника нет; будится следующая доставка чата.

## Редакции и поколения {#редакции-и-поколения}

Закрепление, публикация и разрешение очистки синхронизируются через corpus_state. load_revision_ids используется до Guardrails, затем тот же набор в Searcher. Оба индекса фильтруются по revision_id. Метаданные источника, изображения и готовые ответы живут отдельно от закрепления поисковых точек.

## Хранение и восстановление {#повтор-и-восстановление}

Индексы нужны для активного user_id, готовых state/resume_at, chat_id/delivery_id, case state/check_at и неопубликованного outbox/available_at. Точные DDL/ORM ещё не реализованы. Очистка ledger не может разрешить повтор update, который ещё может повторно прийти; сроки хранения согласуются отдельно. [Outbox](./outbox.md) · [Очистка](./cleanup.md)
