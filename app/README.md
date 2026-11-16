# Каркас RAG-приложения

Добавлен отдельный текстовый канал [Telegram Mini App](../docs/documents/telegram/mini-app.md):
новые HTTP-ручки RAG API, миграция 003_mini_app и поддержка web-жизненного цикла
во внутреннем Runtime. Общие DTO и entrypoints остальных модулей сохранены.
Frontend/backend находятся в соседнем репозитории docs-bots_TG_mini_app.

> [!WARNING]

> Бизнес-сценарии ещё содержат `NotImplementedError`. HTTP-адаптер и последовательность приёма Ingestion Gateway реализованы; конвертер, разбиение и расширения Runtime для DocumentBatch и версий реализованы по согласованным контрактам. Базовые адаптеры Runtime, Model Gateway и RAG Core реализованы; конкретные модельные backend и веса подключаются отдельно. Health checks каркаса подтверждают запуск процессов.


Для локальной проверки есть [адаптер Ollama и небольшой текстовый профиль](../docs/documents/model-gateway/ollama.md); OCR/vision можно добавить явно. Детектор prompt injection — отдельный [сервис на ONNX Runtime](../docs/documents/model-gateway/injection-detector.md) в `injection_detector/` (в образ приложения не входит) с адаптером `rag/model_gateway/injection_detector.py`. После скачивания моделей: `python -m rag.model_gateway.ollama_setup --smoke`. Основной .env и существующий поисковый индекс команда не меняет.

## Где начать

| Модуль | Код | Контракт |
| --- | --- | --- |
| RAG API | [`handle_telegram_update`, `handle_rag_result`, `build_delivery_parts`, `send_answer`, `run_api_maintenance`](rag/rag_api/entrypoints.py) | [Точки входа](../docs/documents/telegram/module/entrypoints.md) |
| Shortcut Interceptor внутри RAG API | [`intercept_message`](rag/rag_api/shortcuts/interceptor.py), [реестр фраз](rag/rag_api/shortcuts/registry.py) | [Точное сопоставление и маршрут reply/operator/miss](../docs/documents/telegram/module/shortcuts.md) |
| Access / Support внутри RAG API | [Допуск](rag/rag_api/access.py), [тикеты и ожидания](rag/rag_api/support.py), [DTO](rag/rag_api/contracts.py) | [Контракты компонентов](../docs/documents/telegram/module/service-entrypoints.md) |
| RAG Core | [`handle_rag_question`](rag/rag_core/entrypoints.py), [Exact Cache](rag/rag_core/exact_cache.py), [Answer Generator](rag/rag_core/answer_generator.py) | [Порядок вызовов](../docs/documents/rag-core/flow.md) |
| Guardrails | [`check_question`](rag/guardrails/entrypoints.py) | [Точка входа](../docs/documents/guardrails/entrypoints.md) |
| Searcher | [`search`](rag/searcher/entrypoints.py) | [Точка входа](../docs/documents/retrieval/entrypoints.md) |
| Ingestion | [`accept_upload`](rag/ingestion/gateway.py), [`convert_document`](rag/ingestion/converter.py), [`handle_ingestion_job`, `prepare_chunks`](rag/ingestion/worker.py) | [Точки входа](../docs/documents/ingestion/entrypoints.md) |
| Model Gateway | [`start_models`, `close_models`, `embed_texts`, `rerank`, `classify_injection`, `generate`, `recognize_text`, `describe_image`](rag/model_gateway/entrypoints.py), [DTO](rag/model_gateway/contracts.py) | [Точки входа](../docs/documents/model-gateway/entrypoints.md) |
| Runtime | [PostgreSQL](rag/runtime/postgresql), [RabbitMQ](rag/runtime/rabbitmq.py), [Queue Relay](rag/runtime/queue_relay.py), [Qdrant](rag/runtime/qdrant.py), [Redis](rag/runtime/redis.py), [временный том](rag/runtime/upload_spool.py), [S3 · изображения](rag/runtime/s3.py), [Telegram-медиа/источники](rag/runtime/media.py), [данные поддержки](rag/runtime/postgresql/support.py) | [Контракты Runtime](../docs/documents/infrastructure/index.md) |

Модельные вызовы находятся в `rag/model_gateway/`; `runtime/qdrant.py` получает готовые векторы и не загружает модели. Runtime реализует адаптеры хранилищ, миграции и запуск фоновых ролей; приём/доставка API, Guardrails и Ingestion реализованы. Административные операции Access/Support и панель `/admin` подключены к Runtime. Readiness рабочего HTTP API проверяет реальные откатываемые записи и свободную ёмкость. Bootstrap не подтверждает готовность бизнес-конвейера и моделей. [Команды Runtime, окружение и интеграционные тесты](../docs/documents/infrastructure/runtime-entrypoints.md).

Model Gateway проверяет все шесть ролей, ограничивает очередь и сроки, собирает stream и предоставляет local_process с управляемыми исполнителями. Для выбранных моделей нужно установить SDK и реализовать фабрики LocalModel; [интерфейс, настройки и проверка check-models](../docs/documents/model-gateway/providers.md). Runtime сохраняет результаты модельных шагов в PostgreSQL для защиты от повторного inference при retry/перезапуске. Перед запуском новой версии примените migrate до актуального head Alembic. Тестовые backend не подключаются в production.

Контракты синхронизированы с текущей документацией: вопрос (RequestRef) отделён от доставки (DeliveryRef), вход поддерживает текст и изображение, итог различает ответ/эскалацию/уведомление. Gateway конвертирует документ и сохраняет полную DocumentBatch до регистрации задания; Worker индексирует готовые чанки с сохранением PreparedRevision. `convert_document` реализован для PDF, DOCX и готового Markdown ([профиль](../docs/documents/ingestion/converter.md#реализованный-профиль)); описания картинок он запрашивает у Model Gateway (роли ocr и vision). Адаптеры Runtime выполняют операции хранилищ и загрузку Telegram-медиа. Расширения Runtime для готовых партий и новой модели версий Ingestion реализованы; выбранные модельные провайдеры и остальные бизнес-сценарии подключаются отдельно.

DTO наследуют Pydantic-базу `rag.dto.DTO`; поля читаются через атрибуты (`result.answer_text`). Правила валидации, JSON и технических потоков — в [справке](../docs/documents/faq/dto.md).

При HTTP-загрузке можно передать query-параметр `source_url` — HTTP(S) ссылку документа для пользователя. Gateway сохраняет её в UploadSource/DocumentBatch, Worker переносит в payload Qdrant, а RAG API использует в источниках ответа. Значение кодируется как query-параметр, например через curl `--url-query 'source_url=https://docs.example/manual?revision=abc&view=1#section'`. Документ по URL не скачивается: тело запроса по-прежнему содержит байты файла. Без source_url сохраняется ссылка на `/sources/{document_id}`.

Общие DTO лежат в [`rag/contracts.py`](rag/contracts.py), специфичные для RAG API и Ingestion — в `contracts.py` внутри своих модулей. Начинайте с тела нужной функции. При изменении сигнатуры согласуйте её с вызывающей стороной и обновите соответствующую страницу документации.

[`tests/test_documented_contracts.py`](tests/test_documented_contracts.py) сверяет точный состав и структуру контрактов с Python-блоками действующей документации. Документ-владелец указан в `Contracts: docs/documents/...md.` в docstring модуля; базовый `rag.dto` описан в справке DTO. При добавлении точки входа или DTO добавьте определение в документ владельца. Тесты сравнивают AST, проверяют DTO на положительных/ошибочных данных и трассируют передачу параметров между точками входа без исполнения бизнес-операций. В tests/test_contracts.py — вопрос и доставка, test_ingestion_contracts.py — загрузка/индексация, test_dto_validation.py — правила полей. К сервисам тесты не подключаются.

У точек входа есть docstring с назначением, параметрами и смыслом результата: в PyCharm откройте Quick Documentation (`Ctrl+Q` в стандартной Windows-раскладке). Они описывают целевой контракт; пометка «Заглушка» означает, что функция пока поднимает `NotImplementedError`. Поля DTO поясняются через `Field(description=...)`, эти же описания доступны в JSON Schema. Поддержка подсказок отдельных полей зависит от IDE и её Pydantic-плагина. При изменении контракта обновляйте и пояснения; подробные протоколы остаются в документации по ссылке в начале файла.

## Python и зависимости

Базовый стек: Pydantic/Settings, FastAPI/Uvicorn, SQLAlchemy/asyncpg/Alembic, aio-pika, Redis, Qdrant Client и HTTPX. Базовые парсеры PDF/DOCX входят в окружение; модельные SDK подключаются для выбранных backend отдельно. Gunicorn и uvicorn-worker доступны опцией `production` для Linux. [Установка и обновление зависимостей](../docs/documents/faq/python-environment.md).

Для встроенных EMF/WMF в DOCX нужен Inkscape на Linux и установленные шрифты. Образ приложения включает `inkscape` и `font-dejavu`; при локальном запуске установите Inkscape и DejaVu системным пакетным менеджером. Изображение отрисовывается из памяти в PNG, затем передаётся в OCR/VLM.

## Запустить локально

Ingestion HTTP запускается отдельно из `app/`: `python -m uvicorn rag.ingestion.application:app --host 127.0.0.1 --port 8001 --no-access-log`. Настройки загрузки — в [корневом `.env.example`](../.env.example); задайте непустой `RAG_INGESTION_TOKEN` в `.env` в корне репозитория. Единственная бизнес-ручка — `POST /admin/ingestion/upload`, [HTTP-контракт и пример запроса](../docs/documents/ingestion/http-upload.md). Приём использует реализованные конвертер, разбиение и Runtime. Сквозная индексация требует отдельно запущенных Queue Relay и Worker, миграций, хранилищ и выбранных модельных провайдеров.

Используйте Python 3.12.x (`.python-version` и `pyproject.toml`). Из папки `app` установите проект: `python -m pip install -c requirements.lock -e .`. Прямые зависимости заданы в `pyproject.toml`, точные базовые версии — в `requirements.lock`. Проверка контрактов: `python -m unittest discover -s tests -v`. Затем выполните из папки `app` по одной команде в двух терминалах.

```text
python -m rag core
python -m rag worker --port 8001
```

Обе роли отвечают на `GET /health/live` и `GET /health/ready`. У worker проверка слушает loopback. Ctrl+C завершает процесс.

## Загрузить документы демонстрации

Для запуска настоящих процессов поверх существующей инфраструктуры есть `scripts/live_services.py`: из `app/` выполните `.venv\Scripts\python.exe scripts/live_services.py api relay results maintenance recovery questions`. Скрипт пересоздаёт только перечисленные контейнеры `rag-live-*`, читает общий `.env` с правильным разбором кавычек и подключает текущий код. Перед первым запуском API остановите старый контейнер каркаса, занимающий порт 8000. По умолчанию используется локальный образ зависимостей `localhost/rag-runtime-tests:local`, сеть `rag-hackathon_default` и spool `rag-hackathon_worker_spool`; параметры можно заменить флагами. Для вопросов предварительно настройте и проверьте модельный профиль. Это локальный стенд разработки, а не production-развёртывание.

Из `app/`: `.venv\Scripts\python.exe scripts/load_demo_documents.py`. Скрипт включает допуск `@verdenkaa` через административный API и отправляет три PDF из `~/Downloads` в Ingestion. Секреты `ADMIN_LOGIN`, `ADMIN_PASSWORD`, `RAG_INGESTION_TOKEN` и публичный URL `RAG_PUBLIC_URL` (совместимый алиас TELEGRAM_WEBHOOK_URL) берутся из общего `.env`; окружение процесса имеет приоритет.

По умолчанию API доступен на `http://127.0.0.1:8000`, Ingestion — на `http://127.0.0.1:8001`; другие адреса задаются через `--api-url` и `--ingestion-url`. Папка файлов задаётся через `--downloads`. Оба HTTP-приложения должны быть запущены отдельно: health-сервер каркаса не предоставляет ручку загрузки.

Файл `.demo-upload-state.json` сохраняет подтверждённые `document_id`/`job_id` и предотвращает повторную отправку тех же файлов этому Ingestion. При неизвестном результате POST скрипт останавливается: перед повтором проверьте логи/БД и соответствующую запись состояния. HTTP 202 означает приём задания; для построения индекса нужны Queue Relay и Worker.

## Запустить через менеджер контейнеров

Для согласованного импорта только текста PDF запустите загрузчик с `--text-only-pdf`: он извлекает Markdown, отправляет `.md` через ту же ручку и сохраняет исходные PDF нетронутыми. Изображения в таком режиме не включаются; VLM не требуется.

Откройте `container-manager/manage.bat` на Windows или запустите `sh container-manager/manage.sh` на Linux/macOS. Выберите пункт `2` (`up app`). С пустыми `CORE_IMAGE` и `WORKER_IMAGE` менеджер сам соберёт недостающие образы каркаса выбранным движком. Токен Telegram для каркаса не требуется.

После правок в `app/` используйте пункт `11` (`Update app images`) для пересборки. Для логов включите `monitor on` или запустите сразу `up full`.

Менеджер показывает stdout через `logs core` и `logs worker`. В Grafana Explore с источником Loki используйте фильтр:

```logql
{service=~"core|worker"} | json | operation="bootstrap" | mode="scaffold"
```

Обе роли пишут `started` при запуске и `finished` при штатной остановке. Общий [`emit_event`](rag/runtime/events.py) принимает поля из [спецификации логов](../docs/documents/faq/logging.md). При реализации передавайте в него реальные ID, исходы и замеры из обработчиков.

Допуск бота задаётся списком Telegram username в БД; User ID остаётся владельцем вопроса. Админ-панель использует статичные ADMIN_LOGIN/ADMIN_PASSWORD из окружения/корневого `.env`; реальные секреты не коммитятся. Runtime реализует новый список допуска, публичное чтение/реплики тикета и операторские операции для доверенного Principal; HTTP-аутентификацию выполняет RAG API: Basic credentials и проверка Origin для мутаций. Callback сверяется с сохранённой кнопкой и квитанцией; гонка с сохранением квитанции требует повтора. prepare_telegram_photo готовит JPEG-копию в отдельном процессе. Для новой БД примените миграции и заполните список username: [создание и применение миграций](../docs/documents/infrastructure/runtime-entrypoints.md#создание-миграций).

Партия `DocumentBatch` содержит полное разбиение одного документа и общие `UploadSource.filename/access_level`. Gateway сохраняет её через `store_document_batch` до регистрации задания. Worker читает `load_document_batch`, вызывает `read_document_version` и `reserve_document_version`, затем готовит оба индекса. Первая версия документа — 0, повтор задания сохраняет назначенный номер. RabbitMQ по-прежнему передаёт только JobRef. Эти операции Runtime реализованы.

## Реализация RAG Core

RAG Core, Exact Cache и Answer Generator реализуют поток из документации. Вопрос
обрабатывается в пределах срока попытки; OCR сохраняется до проверки, временные
ошибки планируют устойчивый повтор, готовый ответ попадает в кеш после коммита.
Searcher реализован через существующие Model Gateway и Qdrant; Guardrails
проверяет вопрос через настроенный детектор Model Gateway и индекс примеров темы.
Роль `questions` отказывает при наличии заглушек с `business_handler_not_implemented`,
проверяет модельные bindings и запускает выбранные провайдеры до потребления очереди.

Политика Core читается из корневого `.env` и окружения при вызове (префикс `RAG_CORE_`):

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `CACHE_TIMEOUT_S` | 0.25 | Общий бюджет чтения/удаления либо записи кеша |
| `OCR_TIMEOUT_S` | 30 | Бюджет OCR, дополнительно ограниченный сроком попытки |
| `GENERATION_TIMEOUT_S` | 60 | Бюджет вызова генерации |
| `MAX_INPUT_BYTES` | 12000 | Бюджет UTF-8 содержимого системного и пользовательского сообщений |
| `MAX_OUTPUT_TOKENS` | 1024 | Бюджет выхода модели |
| `TEMPERATURE` | 0 | Температура генерации |
| `MAX_ATTEMPTS` | 3 | Максимум попыток при временной ошибке |
| `RETRY_DELAY_S` | 5 | Задержка устойчивого повтора |

Бюджет входа — консервативная оценка по байтам, а не токенизатор модели.
При выборе провайдера задайте его с запасом на шаблон чата и выходные токены;
для локального профиля Ollama с контекстом 2048 нужен меньший бюджет.
Фрагменты включаются целиком в порядке поиска; не помещающиеся пропускаются.
Если не помещается ни один, сохраняется технический исход. Модель получает
вопрос и документы отдельным user-сообщением, возвращает JSON с метками S1, S2;
неизвестные источники, изображения и несовпадающие плейсхолдеры отклоняются.

Поведенческие проверки: `python -m unittest discover -s tests -p test_rag_core.py -v`.

## Реализация Searcher

`search` проверяет вход, получает embedding вопроса и сверяет профиль и размерность
с индексом Runtime. Затем вызывает `find_chunks` по переданным редакциям. При
включённом reranker вызывает `rerank`, сопоставляет оценки по `candidate_index`
и сохраняет исходный порядок при равных оценках. Отбор возвращает `SearchFragment`
с исходными метаданными и изображениями. Пустой набор редакций не требует Runtime,
моделей или Qdrant. Ошибки зависимостей сохраняют код и признак повтора.

Лимиты, бюджеты и порог задаются в Runtime через `RAG_SEARCH_CANDIDATE_LIMIT`,
`RAG_SEARCH_TOP_K`, `RAG_EMBEDDING_TIMEOUT_S`, `RAG_RERANK_TIMEOUT_S` и
`RAG_SEARCH_SCORE_THRESHOLD`; reranker включается `RAG_USE_RERANKER=true`.
Значения по умолчанию и шкала порога — в [конвейере поиска](../docs/documents/retrieval/pipeline.md#настройки-searcher).

Зависимости `embed_texts`, `find_chunks`, `rerank`, загрузка редакций Core через
`load_revision_ids`, `check_question`, приём и доставка API реализованы.
Операции `accept_upload`, `prepare_chunks`, `store_document_batch`, `load_document_batch`,
`read_document_version` и `reserve_document_version` также реализованы. Готовность
всего бота требует сквозного прогона с выбранными моделями и внешними сервисами;
готовность HTTP-процесса проверяется отдельной пробой admission.

Проверки: `python -m unittest discover -s tests -p test_searcher.py -v`.

## Создание миграций

Схема описана в `rag/runtime/postgresql/_models.py`, включая индексы и ограничения.
Из `app/` с настроенной БД разработки выполните `python -m rag.runtime migrate`,
затем измените модели и выполните:

```text
python -m alembic revision --autogenerate -m "describe schema change"
python -m rag.runtime migrate
python -m alembic check
```

Проверьте созданный файл перед применением. Alembic использует те же
PGHOST/PGPORT/PGDATABASE/PGUSER/PGPASSWORD, `RAG_PG_SCHEMA` и корневой `.env`,
что и Runtime. Начальная ревизия — `001_initial`; старая неиспользованная история удалена.


Панель Support: `/admin` с HTTP Basic по ADMIN_LOGIN/ADMIN_PASSWORD, через HTTPS
прокси и с публичным origin из RAG_PUBLIC_URL. Посетитель по ссылке тикета
остаётся анонимным даже после входа администратора; оператор пишет отдельной ручкой.
Публичные реплики ограничены 4096 символами, телом 32 КиБ и 20 новыми сообщениями
за минуту на тикет. История выдаётся страницами по 50 реплик.
Миграция `002_ticket_resources` добавляет ограничения/индексы сообщений;
примените `python -m rag.runtime migrate` до запуска новой версии.

## Telegram: служебный polling-вход

telegram-poller запускается по умолчанию. Внешний webhook удалён, внутренний POST /internal/telegram/updates защищён TELEGRAM_INTAKE_SECRET и закрыт публичным nginx. /start, /help, обычный текст и изображения получают только приглашение открыть Mini App через notice/Outbox, остальные slash-команды пропускаются молча. Новых RAG-вопросов этот вход не создаёт; старые callback и ранее принятые вопросы сохраняются.

Контракты /api/v1/* Mini App остаются прежними; его авторизация требует токен бота, но не intake-secret. Общий публичный адрес — RAG_PUBLIC_URL. Старые имена TELEGRAM_WEBHOOK_URL/TELEGRAM_WEBHOOK_SECRET читаются как совместимые env-алиасы, без webhook. Изменения отдельного репозитория Mini App выполняет его владелец.
