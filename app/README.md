# Каркас RAG-приложения

> [!WARNING]
> Рабочий каркас для команды. Прикладные точки входа поднимают `NotImplementedError`. Health checks подтверждают запуск процессов каркаса.

## Где начать

| Модуль | Код | Контракт |
| --- | --- | --- |
| RAG API | [`handle_telegram_update`, `handle_rag_result`, `send_answer`](rag/rag_api/entrypoints.py) | [Точки входа](../docs/documents/telegram/module/entrypoints.md) |
| RAG Core | [`handle_rag_question`](rag/rag_core/entrypoints.py), [Exact Cache](rag/rag_core/exact_cache.py), [Answer Generator](rag/rag_core/answer_generator.py) | [Порядок вызовов](../docs/documents/rag-core/flow.md) |
| Guardrails | [`check_question`](rag/guardrails/entrypoints.py) | [Точка входа](../docs/documents/guardrails/entrypoints.md) |
| Searcher | [`search`](rag/searcher/entrypoints.py) | [Точка входа](../docs/documents/retrieval/entrypoints.md) |
| Ingestion | [`accept_upload`](rag/ingestion/gateway.py), [`handle_ingestion_job`, `prepare_chunks`](rag/ingestion/worker.py) | [Точки входа](../docs/documents/ingestion/entrypoints.md) |
| Model Gateway | [`start_models`, `close_models`, `embed_texts`, `rerank`, `classify_injection`, `generate`](rag/model_gateway/entrypoints.py), [DTO](rag/model_gateway/contracts.py) | [Точки входа](../docs/documents/model-gateway/entrypoints.md) |
| Runtime | [PostgreSQL](rag/runtime/postgresql), [RabbitMQ](rag/runtime/rabbitmq.py), [Queue Relay](rag/runtime/queue_relay.py), [Qdrant](rag/runtime/qdrant.py), [Redis](rag/runtime/redis.py), [временный том](rag/runtime/upload_spool.py) | [Контракты Runtime](../docs/documents/infrastructure/index.md) |

Модельные вызовы находятся в `rag/model_gateway/`; `runtime/qdrant.py` получает готовые векторы и не загружает модели. Все новые операции пока поднимают `NotImplementedError`; bootstrap не вызывает их и не подтверждает готовность моделей.

DTO наследуют Pydantic-базу `rag.dto.DTO`; поля читаются через атрибуты (`result.answer_text`). Правила валидации, JSON и технических потоков — в [справке](../docs/documents/faq/dto.md).

Общие DTO лежат в [`rag/contracts.py`](rag/contracts.py), специфичные для RAG API и Ingestion — в `contracts.py` внутри своих модулей. Начинайте с тела нужной функции. При изменении сигнатуры согласуйте её с вызывающей стороной и обновите соответствующую страницу документации.

[`tests/test_documented_contracts.py`](tests/test_documented_contracts.py) сверяет точный состав и структуру контрактов с Python-блоками действующей документации. Документ-владелец указан в `Contracts: docs/documents/...md.` в docstring модуля; базовый `rag.dto` описан в справке DTO. При добавлении точки входа или DTO добавьте определение в документ владельца. Тесты разбирают примеры без исполнения и не подключаются к сервисам.

У точек входа есть docstring с назначением, параметрами и смыслом результата: в PyCharm откройте Quick Documentation (`Ctrl+Q` в стандартной Windows-раскладке). Они описывают целевой контракт; пометка «Заглушка» означает, что функция пока поднимает `NotImplementedError`. Поля DTO поясняются через `Field(description=...)`, эти же описания доступны в JSON Schema. Поддержка подсказок отдельных полей зависит от IDE и её Pydantic-плагина. При изменении контракта обновляйте и пояснения; подробные протоколы остаются в документации по ссылке в начале файла.

## Python и зависимости

Базовый стек: Pydantic/Settings, FastAPI/Uvicorn, SQLAlchemy/asyncpg/Alembic, aio-pika, Redis, Qdrant Client и HTTPX. Модельные SDK и парсеры пока не входят в базовое окружение. Gunicorn и uvicorn-worker доступны опцией `production` для Linux. [Установка и обновление зависимостей](../docs/documents/faq/python-environment.md).

## Запустить локально

Используйте Python 3.12.x (`.python-version` и `pyproject.toml`). Из папки `app` установите проект: `python -m pip install -c requirements.lock -e .`. Прямые зависимости заданы в `pyproject.toml`, точные базовые версии — в `requirements.lock`. Проверка контрактов: `python -m unittest discover -s tests -v`. Затем выполните из папки `app` по одной команде в двух терминалах.

```text
python -m rag core
python -m rag worker --port 8001
```

Обе роли отвечают на `GET /health/live` и `GET /health/ready`. У worker проверка слушает loopback. Ctrl+C завершает процесс.

## Запустить через менеджер контейнеров

Откройте `container-manager/manage.bat` на Windows или запустите `sh container-manager/manage.sh` на Linux/macOS. Выберите пункт `2` (`up app`). С пустыми `CORE_IMAGE` и `WORKER_IMAGE` менеджер сам соберёт недостающие образы каркаса выбранным движком. Токен Telegram для каркаса не требуется.

После правок в `app/` используйте пункт `11` (`Update app images`) для пересборки. Для логов включите `monitor on` или запустите сразу `up full`.

Менеджер показывает stdout через `logs core` и `logs worker`. В Grafana Explore с источником Loki используйте фильтр:

```logql
{service=~"core|worker"} | json | operation="bootstrap" | mode="scaffold"
```

Обе роли пишут `started` при запуске и `finished` при штатной остановке. Общий [`emit_event`](rag/runtime/events.py) принимает поля из [спецификации логов](../docs/documents/faq/logging.md). При реализации передавайте в него реальные ID, исходы и замеры из обработчиков.
