# Результат настройки и проверки

Проверено 15 ноября 2026 года. Рабочие данные и тома не изменялись.

## Созданные дашборды

| Название | UID / JSON |
| --- | --- |
| RAG Overview | `rag-overview`, `dashboards/rag-overview.json` |
| RAG Pipeline Performance | `rag-pipeline`, `dashboards/rag-pipeline.json` |
| Ingestion & Document Lifecycle | `rag-ingestion`, `dashboards/rag-ingestion.json` |
| Messaging & Reliability | `rag-messaging`, `dashboards/rag-messaging.json` |

Datasource — существующий `rag-loki`. Provisioning создаёт папку RAG, сохраняет
UID, обновляет JSON из Git, не создаёт копии при повторном запуске.

## Источники и добавленное инструментирование

До изменений реально существовали `emit_event` и JSON started/finished,
приём accepted/duplicate, committed result_status Core, duration_ms попыток,
steps[] Core/Worker/API, исходы Guardrails/Cache/Searcher/Generator, модельные
role/profile/execution/wait/inference/reused, публикации Worker с
corpus_generation, timestamps Relay, HTTP-статусы Telegram и исходы доставки.

Добавлены совместимые проекции измеренных шагов `operation=step` с сохранением
исходного steps[] и иерархии; время приём→коммит Core с учётом очереди/ретраев;
result_code/support_reason; время и шаги Gateway; корреляция Worker/model;
разделение inference и postprocess; числовое ожидание готовой outbox и отдельные
broker_confirmed/outbox_marked; успешные consume и redelivery; число фактических
восстановленных уведомлений. Публичные DTO и сигнатуры не изменены.

## Что проверено

- Полный `app` unittest discovery: **229 тестов, OK, 31 skipped**. После последних
  изменений проекции полный запуск повторён: 36.487 с. Интеграционные проверки
  без внешних ресурсов/окружения пропущены штатно. Локальная среда — Python 3.14.7
  из `app/.venv`; проект объявляет Python 3.12, прогон именно на 3.12 здесь не выполнен.
- Отдельно API logging/startup: 14 тестов; Core: 24; новые тесты проекции: 2.
- Менеджер контейнеров: 14 тестов; документация: `npm test` — 7, `npm run build` — OK.
  На Windows использовался `npm.cmd`, поскольку выполнение npm.ps1 запрещено политикой PowerShell.
- Docker Compose с временным тестовым env: `config --quiet` — exit 0.
  Рабочий `.env` не читался и не менялся.
- Alloy 1.8.3 разобрал неизменённую конфигурацию через `fmt`, без записи файла.
- Реальные контейнеры Loki 3.5.8 и Grafana 12.1.1 в отдельной временной сети:
  загрузка четырёх дашбордов, datasource health, повторный запуск с теми же ID — OK.
- **128 выражений LogQL**: разбор, отсутствие искусственных значений на пустом
  Loki, выполнение на контрольных событиях — OK. Проверены реальные targets из JSON:
  уникальные вопросы, исключение started/ретраев/stale, глобальные p50/p95/p99,
  проценты, нулевой числитель при наблюдаемом знаменателе, отсутствие знаменателя,
  cache hit rate, модельные замеры, Worker, Relay и Telegram 429.
- Сумма верхних шагов на контрольном примере — 660 мс; вложенные 480 мс не прибавляются.
- Фильтры service/component/operation/route, точные ID и объединение request_id
  с delivery_id проверены запросами к Loki. ResultNotice Relay связывается через
  delivery_id, без добавления несуществующего request_id в DTO.
- В тестовый Loki воспроизведены **10 событий настоящего обработчика Core**,
  исполненного с инфраструктурными зависимостями из существующего test fixture.
  Это исполнение прикладного кода с подменёнными хранилищами/моделями, не production-трафик.
- В браузере проверен Overview: три принятых вопроса, два нетехнических итога,
  по одному answered/support/technical_error; 33.3% answered; p50=10 с, p95=28 с,
  p99=29.6 с; 66.7% до 20 секунд. Проверены оформление, цвет ошибки, единицы и
  отрисованные ссылки Explore с интерполированными фильтрами/диапазоном.
  Полный интерактивный обход всех четырёх экранов и открытие Explore не выполнялись.
- Тестовые контейнеры и сеть удалены; постоянные тома не создавались.
- `git diff --check` — без ошибок.

В существующих `core`/`worker` за последние 24 часа найдены только события
`bootstrap` scaffold-образов: по 5 started/ready и 4 finished/stopped.
Исторического бизнес-трафика для проверки production-показателей нет.
Пустые workload-панели на этих логах ожидаемы и не заменяются тестовыми значениями.

## Ограничения и применение

Активные обработки и backlog RabbitMQ не выводятся из разности логов.
Полное время загрузка→публикация и вопрос→успешная доставка нельзя надёжно
получить из текущих данных, доступных обработчикам. Reconciliation показывает
наблюдавшиеся переходы, а не текущую задолженность. Причины и минимальные способы
расширения описаны [в руководстве](../../docs/documents/faq/monitoring.md).
Логи не транзакционны с БД; потеря stdout после коммита может занизить счётчики.
Перцентили полагаются на единственный идемпотентный терминальный коммит.

Для существующего рабочего стенда нужны обычный Compose up с пересозданием
Grafana для новых mounts и обновление образов приложения для новой телеметрии.
Во время этой задачи рабочие контейнеры не пересоздавались, модели не загружались.
Loki/Alloy/datasource конфигурации оставлены прежними; новых сервисов нет.

## Изменённые и добавленные файлы

Мониторинг:

- `container-manager/compose.yaml`
- `container-manager/monitoring/dashboards.yaml`
- `container-manager/monitoring/dashboards/rag-overview.json`
- `container-manager/monitoring/dashboards/rag-pipeline.json`
- `container-manager/monitoring/dashboards/rag-ingestion.json`
- `container-manager/monitoring/dashboards/rag-messaging.json`
- `container-manager/monitoring/generate_dashboards.py`
- `container-manager/monitoring/verify.py`
- `container-manager/monitoring/README.md`
- `container-manager/monitoring/VALIDATION.md`

Инструментирование:

- `app/rag/runtime/events.py`
- `app/rag/runtime/queue_relay.py`
- `app/rag/runtime/rabbitmq.py`
- `app/rag/runtime/runner.py`
- `app/rag/rag_core/entrypoints.py`
- `app/rag/rag_api/_observability.py`
- `app/rag/ingestion/gateway.py`
- `app/rag/ingestion/worker.py`
- `app/rag/model_gateway/_manager.py`

Тесты:

- `app/tests/test_monitoring_events.py`
- `app/tests/test_monitoring_runtime.py`
- `app/tests/test_logging.py`
- `app/tests/test_rag_core.py`
- `app/tests/test_model_gateway.py`
- `app/tests/test_ingestion_gateway_http.py`
- `app/rag/rag_api/tests/test_logging.py`
- `app/rag/ingestion/_tests/test_worker.py`

Документация:

- `docs/documents/faq/logging.md`
- `docs/documents/faq/monitoring.md`
