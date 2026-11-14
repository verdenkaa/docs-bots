---
title: Логирование запросов
sidebar_position: 3.5
---

:::warning Рабочая спецификация
Формат и точки записи заданы для MVP. На первом сквозном прогоне проверим полноту событий и точность замеров между процессами.
:::

Пишем структурированные логи в stdout, по одному JSON-объекту на строку. Каждый обработчик накапливает шаги в контексте своей попытки и пишет итоговую запись в `finally`. Путь вопроса собираем в Grafana по request_id; самостоятельные уведомления дополнительно связываем по delivery_id и case_id: приём, обработка и доставка могут выполняться в разное время, в разных процессах и повторяться независимо. [Как открыть логи в Grafana](./container-manager.md)

## Кто что записывает

| Компонент | Когда и что пишем |
| --- | --- |
| **RAG API · приём** | Одна запись на входящий Telegram update из выбранного webhook. Шаги включают проверку входа и `register_question`. Исходы различают принятие, повторный update, пропуск и ошибку. До регистрации доступен `update_id`, после неё добавляем `request_id`. Для HTTP записываем метод, шаблон маршрута и статус ответа. |
| **RAG Core · обработка** | Одна итоговая запись на вызов `handle_rag_question`. В `steps` добавляем каждый фактический вызов функций [пайплайна](../rag-core/flow.md), от `claim_question` до `complete_question`, `store_answer` или планирования повтора. Записываем исходы ветвлений, например `blocked`, `cache_hit` и `no_fragments`. Отдельно отмечаем `result_status` для зафиксированного итога и `consume_decision` для входящего уведомления. |
| **Model Gateway · вызовы** | В контекст текущей попытки Core/Worker добавляем вложенный шаг: `model_role`, `profile_id`, `execution`, `wait_ms` (ожидание допуска), `inference_ms` (исполнение), исход и доступные token usage. Время входит в родительский шаг Guardrails/Searcher/Answer Generator; при суммировании вложенные длительности повторно не прибавляем. Промпты, тексты, ответы и секреты по умолчанию не пишем. Для запуска/остановки — отдельные технические события. [Правила](../model-gateway/lifecycle.md). |
| **Ingestion Gateway / Worker** | Gateway пишет приём/convert_document/OCR/VLM/put_image/stage_upload/register_revision. Worker — claim, чтение/сохранение PreparedRevision, чанки/примеры, обе записи/проверки Qdrant и публикацию. Связь по job_id/revision_id и attempt_no; байты и тексты не логируются. |
| **Queue Relay · публикация** | Одна запись на попытку публикации для каждого из трёх маршрутов. Указываем маршрут, `message_id`, `request_id`, `delivery_id` или `job_id`, `available_at` из [`ReadyOutboxRow`](../infrastructure/postgresql/outbox.md#list-ready-outbox), время начала `publish_notice` и исходы подтверждения брокером и записи `published_at`. |
| **RAG API · Support** | Фиксирует offer/start/confirm/decline, due-переход, создание тикета и case_id; повторное действие не считается новым кейсом. |
| **RAG API · доставка** | Одна итоговая запись на вызов `handle_rag_result`. Шаги включают `claim_delivery`, каждый исходящий вызов Telegram с именем операции, вариантом сообщения и кодом ответа, сохранение исхода или планирование повтора. Различаем доставку, отказ, повтор и неизвестный исход. |

Адаптер RabbitMQ фиксирует время получения и AMQP `message_id` в контексте логирования до вызова обработчика. Бизнес-DTO остаются прежними. Для Core и доставки сразу пишем короткое событие `started` с уникальным `run_id` и тем же ID запроса. Итоговая запись `finished` использует тот же `run_id`. Если процесс оборвался, по событию `started` видно, где началась попытка без итоговой записи. Последний вызванный шаг при аварийном завершении может быть неизвестен.

## Общий формат

| Поля | Что означают |
| --- | --- |
| `schema_version`, `component`, `operation`, `event` | Версия формата, владелец, имя обработчика или операции, `started` либо `finished`. |
| `run_id`, `request_id`, `delivery_id`, `case_id`, `job_id`, `update_id`, `message_id`, `route`, `attempt_no` | Уникальный ID запуска и доступные ключи связи. `attempt_no` читаем после взятия работы и рассматриваем вместе с операцией, поскольку обработка и доставка нумеруются отдельно. Неизвестные поля опускаем. |
| `started_at`, `received_at`, `finished_at`, `duration_ms` | Моменты в UTC, включая время получения уведомления, когда оно доступно. Длительность внутри процесса измеряем монотонными часами. |
| `outcome`, `error_code` | Исход этапа и стабильный технический код ошибки при наличии. |
| `result_status`, `consume_decision` | Для Core — тип сохранённого результата по ветке пайплайна и решение для входящего уведомления. |
| `available_at`, `publish_started_at` | Для Relay — время готовности записи outbox и начало вызова брокера в UTC. |
| `steps[]` | В порядке начала вызовов. Для каждого — `name`, `offset_ms` от начала обработчика, `duration_ms`, `outcome`, при ошибке `error_code`. Повторные вызовы получают отдельные элементы. Для доставки записываем delivery_id, purpose, part_no, Telegram message_id и вариант Sent.variant. Для `sendMessage` добавляем `delivery_variant` со значением `answer` или `length_fallback`. |

Пример итоговой записи Core при попадании в кеш. В stdout объект записывается одной строкой.

~~~json
{
  "schema_version": 1,
  "component": "rag_core",
  "operation": "handle_rag_question",
  "event": "finished",
  "run_id": "5a096190-3a3f-4fd2-848d-d33648d41816",
  "request_id": 1042,
  "message_id": "581d505d-d09d-482b-aa88-11406ed5ae42",
  "route": "rag.questions",
  "attempt_no": 1,
  "received_at": "2026-11-10T12:00:00.000Z",
  "started_at": "2026-11-10T12:00:00.001Z",
  "finished_at": "2026-11-10T12:00:00.019Z",
  "duration_ms": 18,
  "outcome": "completed",
  "result_status": "answered",
  "consume_decision": "ack",
  "steps": [
    {"name": "claim_question", "offset_ms": 0, "duration_ms": 4, "outcome": "claimed"},
    {"name": "load_revision_ids", "offset_ms": 4, "duration_ms": 1, "outcome": "loaded"},
    {"name": "check_question", "offset_ms": 5, "duration_ms": 6, "outcome": "allowed"},
    {"name": "get_answer", "offset_ms": 11, "duration_ms": 3, "outcome": "cache_hit"},
    {"name": "complete_question", "offset_ms": 14, "duration_ms": 4, "outcome": "committed"}
  ]
}
~~~

В логи кладём идентификаторы, числовые метрики и коды. Тексты вопросов и ответов, фрагменты документов, тело Telegram update, URL с токенами, заголовки авторизации и сырые тексты исключений в логи не попадают.

## Как проверить путь и найти задержку

1. Отфильтруйте JSON по `request_id`. Для конкретной доставки сопоставьте Relay и потребителя по маршруту и `message_id`. Для попыток обработки и доставки используйте `operation`, `attempt_no` и `run_id`.
2. Сверьте последовательность шагов с веткой пайплайна. В примере `cache_hit` объясняет отсутствие поиска и генерации. `complete_question=committed` подтверждает сохранение результата. Доставку подтверждает отдельная запись RAG API.
3. Внутри этапа сравните `duration_ms` шагов. Для каждого маршрута интервал от `available_at` до начала публикации показывает ожидание готовой записи outbox. Интервал от начала публикации до `received_at` даёт оценку времени передачи и ожидания у брокера. При повторной публикации с тем же ID точное разделение задержек может быть недоступно. Для сравнения времени между процессами нужны синхронизированные часы.
4. Для сквозной задержки возьмите время от первого приёма принятого update до успешного ответа Telegram. Сравнивайте отдельно обычный путь, попадания в кеш и запросы с повторами.

Пример выше иллюстративный, не измерение. Для изображения перед load_revision_ids дополнительно видны получение файла, recognize_text и save_prepared_question. Для topic gate внутри check_question — embedding и find_topic_matches. Для принятого кеш-попадания основные Searcher/Answer Generator отсутствуют, но фильтр темы уже выполнен. Превышение целевых 20 секунд фиксируется метрикой, не outcome эскалации.
