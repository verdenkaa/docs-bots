---
title: Жизненный цикл Runtime
sidebar_position: 2.1
toc_max_heading_level: 2
---

Runtime не создаёт клиентов и не запускает сервисы при импорте. Конфигурация читается из окружения в момент явного запуска; секреты не входят в DTO и сообщения диагностики. Прикладные обработчики остаются у владельцев RAG API/Core/Ingestion/Model Gateway.

Слой PostgreSQL использует ORM-модели и отдельную AsyncSession на транзакцию. SQLAlchemy формирует запросы, условия переходов и блокировки; бизнес-модули получают только DTO. [Устройство слоя хранения](./postgresql/index.md#слой-хранения).

## <span className="operation-badge operation-badge--async">ASYNC</span> `migrate_runtime` {#migrate-runtime}

~~~python
async def migrate_runtime() -> None: ...
~~~

Оператор применяет версионированные миграции PostgreSQL отдельным процессом до запуска приложения. Миграции выполняются транзакционно под advisory lock; повтор не создаёт таблицы заново. `RAG_PG_SCHEMA` выбирает схему (по умолчанию public). У миграционного пользователя должны быть права DDL. Миграция не удаляет существующие данные и не запускается автоматически каждым HTTP worker.

## <span className="operation-badge operation-badge--async">ASYNC</span> `start_runtime` {#start-runtime}

~~~python
async def start_runtime() -> None: ...
~~~

Сборка процесса проверяет схему PostgreSQL и создаёт асинхронные клиенты хранилищ/HTTP. Повторный, в том числе одновременный, запуск в том же процессе не создаёт вторую копию ресурсов. При ошибке освобождаются уже созданные клиенты. RabbitMQ подключается и объявляет три долговечные очереди при первой публикации или запуске потребителя: его отказ не блокирует сохранение вопроса с outbox. Redis не обязан отвечать при создании клиента: его отказы переводятся в CacheUnavailable при вызове операции.

## <span className="operation-badge operation-badge--async">ASYNC</span> `close_runtime` {#close-runtime}

~~~python
async def close_runtime() -> None: ...
~~~

Закрывает все ресурсы процесса, включая частично созданные; повтор безопасен. Ошибка закрытия одного клиента не пропускает остальные. Внешний супервизор ограничивает время остановки процесса.

## <span className="operation-badge operation-badge--async">ASYNC</span> `prepare_runtime_storage` {#prepare-runtime-storage}

~~~python
async def prepare_runtime_storage() -> None: ...
~~~

После start_runtime проверяет совместимость embedding-профиля PostgreSQL и обеих коллекций Qdrant. Пустые коллекции/индексы создаёт только для нового корпуса. Существующие несовместимые коллекции не пересоздаются; утрата или неполнота нужной редакции даёт qdrant_recovery_required. Создаёт настроенный приватный S3-bucket при его отсутствии. Профиль, размерность и имена коллекций задаются RAG_EMBEDDING_PROFILE, RAG_EMBEDDING_DIMENSIONS, RAG_CHUNKS_COLLECTION и RAG_TOPICS_COLLECTION.

## <span className="operation-badge operation-badge--async">ASYNC</span> `run_runtime` {#run-runtime}

~~~python
async def run_runtime(component: Literal['relay', 'recovery', 'questions', 'results', 'maintenance', 'ingestion']) -> None: ...
~~~

Запускает одну фоновую роль до SIGTERM/SIGINT, затем прекращает приём, ограниченно ждёт обработчики и закрывает модели и клиентов. Потребители передают валидированный RequestRef, DeliveryRef или JobRef владельцу бизнес-логики. Повреждённое уведомление подтверждается без вызова обработчика; неизвестное состояние PostgreSQL возвращает сообщение брокеру. У ingestion prefetch=1. Relay, recovery, maintenance и ingestion защищены отдельными сессионными advisory lock от второго активного экземпляра.

Роль questions выбирает embedding/generation/injection/ocr и, при RAG_USE_RERANKER=true, reranker; ingestion выбирает embedding/generation. RAG_MODEL_BINDINGS содержит JSON-карту ModelRole → ModelBinding. В ней указывается имя переменной секрета, а не значение ключа. Embedding-профиль и размерность должны совпасть с индексом. HTTP, Relay и recovery не запускают модели. Явные заглушки бизнес-обработчиков отклоняются с business_handler_not_implemented до потребления сообщений. Реализация Runtime не заменяет ещё не реализованные RAG API/Core/Ingestion/Model Gateway.

## Операционные команды

Запускать из app/ с установленными зависимостями:

~~~sh
python -m rag.runtime migrate
python -m rag.runtime prepare
python -m rag.runtime set-access @employee_name
python -m rag.runtime set-access employee_name --disable
python -m rag.runtime serve relay
python -m rag.runtime serve recovery
python -m rag.runtime serve questions
python -m rag.runtime serve results
python -m rag.runtime serve maintenance
python -m rag.runtime worker
python -m rag.runtime pending-writes --limit 100
~~~

Администратор задаётся ADMIN_LOGIN/ADMIN_PASSWORD, а не записью Telegram в БД; прежняя команда bootstrap-admin удалена. set-access — локальная операция оператора развёртывания с настроенными административными credentials в окружении: нормализует имя и вызывает тот же write_user_access. HTTP-адаптер проверяет пароль до создания Principal; Runtime не принимает Principal из публичной формы. pending-writes только показывает истёкшие неизвестные записи, ничего не удаляет. Их сверка выполняется по [операторской процедуре](./qdrant/index.md#сверка-незавершённых-записей), автоматического снятия qdrant_write_pending нет.

### Обновление со схемы 001_runtime

Перед запуском обновлённого кода остановите процессы приложения и выполните migrate. Миграция 002_username_access сохраняет вопросы, доставки, outbox и историю. Числовой Telegram User ID не позволяет восстановить текущее имя: старый непустой employees архивируется в legacy_employee_access, а новый список username заполняется через set-access/Access. Автоматического допуска по старому ID нет. Исторические rag_requests сохраняют username=NULL; новые принятые вопросы содержат снимок разрешённого username. start_runtime отклоняет старую схему до создания клиентов и потребителей.

У прежних реплик сохраняются message_id, текст и время; исходные author_id/author_role/client_message_id остаются в технических legacy-полях. Прежние операторы обозначаются author_login=legacy-telegram:ID: это историческая метка, а не новый административный логин. При совпадении прежних ключей у разных авторов сохраняются все сообщения; последующим конфликтующим репликам назначаются устойчивые новые client_message_id. Автоматический downgrade этой смены идентичности запрещён; откат требует восстановления оператором.

### Кнопки Support

Внутренний формат callback_data разделяется сборщиком DeliveryPart и адаптером Telegram: d:request_id — offer(dissatisfied), h:request_id — offer(human_request), s:request_id — start, y:case_id — confirm, n:case_id — decline. UUID записывается канонически с дефисами; все варианты укладываются в 64 байта. Пример s:1042 сохраняется. Кодек находится в runtime/postgresql/_callbacks.py; он не заменяет проверку источника Telegram.

Runtime сверяет действие с кнопкой конкретной сохранённой TextPart, квитанцией именно этой части, владельцем, чатом и request/case. При гонке с отправкой возвращает retry_later без ledger; после сохранения квитанции тот же update может примениться. Чужой или устаревший callback сохраняется как ignored. Для текстовой просьбы offer сверка кнопки не нужна. Незавершённый контракт признака заранее подготовленного length_fallback остаётся [открытым](../telegram/module/entrypoints.md); Runtime не определяет variant по тексту и не меняет DTO самовольно.

Команда worker предназначена для Linux-контейнера. Родитель запускает потребителя в отдельной группе процессов, получает короткий бюджет claim, затем срок взятой попытки и бюджеты PostgreSQL/Qdrant/файлов. По монотонному сроку останавливает всю группу через SIGTERM, затем SIGKILL. Новый потребитель запускается только после подтверждения остановки выполняющихся процессов прежней группы. Неизвестный исход Qdrant сохраняется для сверки. Прямой запуск serve ingestion без супервизора запрещён. Модельный адаптер должен соблюдать лимит шага по контракту Model Gateway; общий срок дополнительно контролируется родителем.

recovery восстанавливает все три маршрута, убирает завершённый staging и разрешённые PostgreSQL редакции обоих индексов. S3-изображения и метаданные источников не удаляются. Ошибка прохода оставляет работу следующему проходу; Relay отмечает публикацию только после подтверждения маршрутизации брокером.

## Окружение и проверки

Адреса и секреты: PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD, REDIS_URL, RABBITMQ_HOST/RABBITMQ_PORT/RABBITMQ_USER/RABBITMQ_PASSWORD, QDRANT_URL, RAG_QDRANT_API_KEY, S3_ENDPOINT_URL/S3_ACCESS_KEY/S3_SECRET_KEY/S3_REGION, TELEGRAM_BOT_TOKEN, ADMIN_LOGIN/ADMIN_PASSWORD. WORKER_SPOOL_DIR указывает постоянный рабочий том. Полный набор параметров и безопасные значения по умолчанию — в app/rag/runtime/_config.py. Настройки читаются при явном запуске из окружения и локального .env текущего каталога; явное окружение имеет приоритет. Реальные секреты не коммитятся.

prepare_telegram_photo использует отдельный завершаемый процесс с Pillow 13.0.0: EXIF-ориентация, белый фон для прозрачности, RGB/JPEG, поля без обрезания, уменьшение размеров и подбор качества. S3-оригинал не меняется. Анимация даёт первый кадр. RAG_PHOTO_MAX_PIXELS (по умолчанию 25 миллионов) ограничивает декодирование, RAG_PHOTO_MEMORY_BYTES (512 MiB) задаёт жёсткий лимит адресного пространства Linux-исполнителя, RAG_PHOTO_TIMEOUT_S (15 секунд) дополнительно ограничен остатком DeliveryAttempt. На процесс Runtime одновременно выполняется одно преобразование; отмена/таймаут останавливает и дожидается исполнителя. На Windows доступны пиксельный лимит, последовательность и остановка процесса; жёсткий лимит памяти применяется в рабочем Linux-контейнере.

Лимиты задаются RAG_IO_TIMEOUT_S, RAG_QUESTION_TIMEOUT_S, RAG_DELIVERY_TIMEOUT_S, RAG_INGESTION_TIMEOUT_S, RAG_MAX_PENDING_REQUESTS, RAG_STARTUP_TIMEOUT_S и RAG_SHUTDOWN_TIMEOUT_S. Для Redis используется RAG_CACHE_TTL_S. RAG_S3_BUCKET по умолчанию rag-images. Поиск RAG_SEARCH_MODE=hybrid использует серверный weighted RRF с BM25; dense явно отключает лексическую ветку.

Из корня репозитория интеграционные проверки запускаются в отдельном Compose-проекте без опубликованных портов и общих томов:

~~~sh
docker compose -p rag-runtime-tests -f app/tests/runtime-compose.yaml up -d --wait
docker compose -p rag-runtime-tests -f app/tests/runtime-compose.yaml build runner
docker compose -p rag-runtime-tests -f app/tests/runtime-compose.yaml run --rm --no-deps runner
docker compose -p rag-runtime-tests -f app/tests/runtime-compose.yaml down
~~~

Для обычных контрактных и модульных проверок из app/: python -m unittest discover -s tests -v. Интеграционные тесты включаются только RUN_RUNTIME_INTEGRATION=1 и создают уникальные схемы, коллекции, очереди и bucket для каждого теста. Health-сервер python -m rag core/worker сохраняет режим scaffold и не подтверждает готовность бизнес-бота.
