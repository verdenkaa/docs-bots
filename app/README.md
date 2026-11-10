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
| Runtime | [PostgreSQL](rag/runtime/postgresql), [RabbitMQ](rag/runtime/rabbitmq.py), [Queue Relay](rag/runtime/queue_relay.py), [Qdrant](rag/runtime/qdrant.py), [Redis](rag/runtime/redis.py), [временный том](rag/runtime/upload_spool.py) | [Контракты Runtime](../docs/documents/infrastructure/index.md) |

Общие DTO лежат в [`rag/contracts.py`](rag/contracts.py), специфичные для RAG API и Ingestion — в `contracts.py` внутри своих модулей. Начинайте с тела нужной функции. При изменении сигнатуры согласуйте её с вызывающей стороной и обновите соответствующую страницу документации.

## Запустить локально

С Python 3.12 выполните из папки `app` по одной команде в двух терминалах.

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
