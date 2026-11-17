---
title: Серверный запуск одной командой
sidebar_position: 2.2
---

:::warning Проверка на целевом сервере
Конфигурация рассчитана на Ubuntu 24.04, CPU, 32 ядра и 64 GiB RAM. Выпуск настоящего сертификата, доступность Telegram, скорость и качество выбранных шести моделей проверяются на сервере.
:::

## Первый запуск

Установите Docker Engine и Compose plugin по [инструкции Docker для Ubuntu](https://docs.docker.com/engine/install/ubuntu/). Docker должен запускаться вместе с сервером. Требуется Compose 2.24 или новее с поддержкой `env_file.required`.

Получите репозиторий через Git. Положите подготовленный `.env` **в корень checkout**, рядом с `compose.yaml`. Этот файл и резервная копия `.env.before-server` исключены из Git и Docker build context. Секреты не входят в образы.

Выполните из корня:

```bash
chmod 600 .env
docker compose config --quiet
docker compose up -d --build
docker compose ps -a
docker compose logs --tail 100 validate migrate models-init prepare nginx telegram-poller
```

`validate`, `migrate`, `models-init`, `prepare` после успеха имеют статус `Exited (0)`: это нормально для подготовительных задач. `core`, `worker`, `nginx` должны стать `healthy`. Первый старт включает скачивание образов и весов и занимает дольше повторного.

Перед переключением бота с тем же токеном остановите его локальные процессы обработки и доставки. `telegram-poller` удаляет прежнюю регистрацию webhook без потери накопленных updates и запускает getUpdates. Для независимого теста используйте отдельного бота и отдельное окружение.

## Адрес и сертификат без домена

### Telegram: только long polling {#long-polling-вместо-входящих-webhook-запросов}

`telegram-poller` запускается обычным `docker compose up -d --build`, отдельный профиль не требуется. Внешняя регистрация webhook и сервис `webhook` удалены. Старое `TELEGRAM_UPDATE_MODE=polling` допустимо, переменную можно убрать. Значение webhook отклоняется проверкой конфигурации. COMPOSE_PROFILES управляет только дополнительными модельными сервисами, например native.

Poller вызывает deleteWebhook с drop_pending_updates=false, получает getUpdates с ожиданием 30 секунд и передаёт Update на внутренний `http://core:8000/internal/telegram/updates` с `X-Telegram-Intake-Secret`. Публичный nginx закрывает `/internal/*` и прежний `/telegram/webhook`. Telegram-вход создаёт только notice с приглашением открыть Mini App для `/start`, `/help`, обычного текста и изображений; прочие slash-команды сохраняются молча. Старые callback сохраняют прежнюю обработку. Новые RAG-вопросы принимает только `/api/v1/questions`, его контракт не изменён.

После HTTP 200 poller увеличивает offset. При потере ответа или рестарте повтор безопасен по ledger update_id. Навигация, ledger и outbox записываются одной транзакцией; старые принятые вопросы не заменяются новым уведомлением. Ошибки сети/503 повторяются; 429 учитывает retry_after; ошибка токена, конкурирующий получатель или постоянный отказ intake останавливают процесс без подтверждения update. Lock в общем spool допускает один poller; второй экземпляр того же бота в другом стенде запускать нельзя.

Настройте `TELEGRAM_INTAKE_SECRET` (внутренний секрет) и `RAG_PUBLIC_URL=https://PUBLIC_HOST` (ссылки, авторизация Origin и публичный HTTPS). Старые TELEGRAM_WEBHOOK_SECRET/TELEGRAM_WEBHOOK_URL читаются как алиасы: существующий секретный .env не требуется переписывать в момент обновления. Mini App проверяет initData по TELEGRAM_BOT_TOKEN и не требует intake-secret; HTTP-контракты Mini App сохранены.

```bash
docker compose ps -a telegram-poller
docker compose logs --tail=50 telegram-poller
```

При обновлении прежнего стека сначала остановите старый poller и webhook-задачу старой версией Compose; затем обновите код и выполните `docker compose up -d --build --remove-orphans`. Удалённый webhook-контейнер не должен оставаться самостоятельным получателем. Данные и тома не удаляйте, drop_pending_updates=true не используйте. Для секретов/нового окружения нужен пересозданный контейнер, restart не перечитывает env. Отдельный репозиторий Mini App обновляет его владелец; его API и адрес RAG_API_BASE_URL остаются прежними.

При сетевой блокировке нужен исходящий доступ к Bot API и загрузке файлов Telegram также из контейнеров доставки и Core. На целевом сервере он обеспечен отдельным Amnezia-туннелем: только TCP/443 к сетям Telegram, основной маршрут и DNS сохранены. Управление: `systemctl status docs-bots-telegram-vpn`; эксплуатационная заметка — `/root/.config/docs-bots-vpn/README.md`. Ключи VPN находятся вне checkout и не входят в Git или Docker-образы. При переносе на другой VPS сетевой туннель настраивается отдельно.

`PUBLIC_HOST` — публичный IPv4 либо DNS-имя. `RAG_PUBLIC_URL` должен быть точно `https://PUBLIC_HOST`, без пути и порта. nginx сначала обслуживает только HTTP-проверку ACME, затем получает доверенный сертификат через Certbot и включает HTTPS.

Для IPv4 применяется `--ip-address` с профилем `shortlived`: сертификат действует около шести дней. Проверка продления выполняется каждые шесть часов; при ошибке повтор через пять минут. После обновления сертификата nginx перезагружает конфигурацию. Certbot 5.10 закреплён в образе, поэтому поддержка IP не зависит от старого пакета Ubuntu. [Сертификаты IP в Certbot](https://letsencrypt.org/2026/03/11/shorter-certs-certbot.html).

`ACME_EMAIL` необязателен: при пустом значении аккаунт создаётся без email. `ACME_DIRECTORY_URL` по умолчанию указывает на production Let’s Encrypt. Staging выдаёт недоверенный сертификат и не подходит для публичного интерфейса.

Откройте TCP 80/443 в firewall провайдера, firewall Ubuntu и, если сервер за NAT, в правилах перенаправления. Порт 80 нужен и для продления. Настройте время сервера; IP должен оставаться закреплённым за сервером.

## Порядок запуска и готовность

1. `validate` проверяет настройки без подключения к сервисам и вывода секретов.
2. PostgreSQL, RabbitMQ, Redis, Qdrant и RustFS обеспечивают состояние во внутренней сети.
3. `migrate` применяет существующие миграции PostgreSQL под advisory lock.
4. Ollama, `native-models` и `injection-detector` загружают выбранные модели. `models-init` ждёт готовности, скачивает недостающие Ollama-веса, сверяет SHA-256 и вызывает `start_models` для настоящих синтетических inference-проб всех ролей через Model Gateway. Несовпадение версии блокирует запуск; профиль автоматически не переписывается.
5. `prepare` проверяет обе коллекции Qdrant и приватный bucket S3, применяет необязательный первоначальный допуск.
6. `core` запускает HTTP API и отдельно `relay`, `recovery`, `questions`, `results`, `maintenance`. `worker` запускает Ingestion HTTP и индексатор под супервизором попыток. Оба используют общий spool.
7. nginx включает HTTPS для Mini App/API и публичных ссылок; telegram-poller получает updates исходящими getUpdates и использует внутренний intake.
8. Grafana, Loki и Alloy запускаются по умолчанию; четыре технических дашборда и источник Loki подключаются provisioning. После `migrate` одноразовый `analytics-init` создаёт аналитические представления и ограниченного PostgreSQL-читателя; Grafana ждёт его и дополнительно подключает [бизнес-дашборд](../faq/business-analytics.md). Модели для аналитики не запускаются.

HTTP обслуживается Gunicorn с `uvicorn-worker`. `API_WORKERS` по умолчанию 2, `INGESTION_HTTP_WORKERS` — 1. HTTP workers не запускают потребителей очередей. Контейнерный супервизор завершает роль при потере обязательного процесса, затем Docker её перезапускает. Health проверяет живые PID, heartbeat, HTTP и локальные сигналы готовности потребителей/singleton-ролей. Это не проверка качества ответов или отсутствия задержек очередей.

## Модели и CPU

Источник выбора моделей — `RAG_MODEL_BINDINGS` в `.env`; закреплённый серверный набор приведён в `deploy/model-profile.json`. Подготовка использует активные роли и не скачивает выключенный reranker. Изменившийся Ollama-тег не принимается под прежним профилем. Для новой модели обновляются также `model_revision`, `profile_id` и связанные профили приложения.

| Задача | Выбранная модель | Исполнение |
| --- | --- | --- |
| Ответ в JSON | `qwen3.5:4b-q4_K_M` | Ollama, контекст 16384, thinking выключен, JSON включён. |
| Описание изображений | `qwen3-vl:4b-instruct-q4_K_M` | Ollama, контекст 8192. |
| Векторизация | `bge-m3:567m` | Ollama, dense 1024, без инструкций EmbeddingGemma. |
| OCR | PaddleOCR PP-OCRv5 | CPU: server detector + официальный eslav recognizer для русского/английского. |
| Reranking | `BAAI/bge-reranker-v2-m3` | CPU Transformers, sigmoid score. |
| Защита от инъекций | `Horizon-Labs/prompt-injection-guard-base` | Существующий ONNX backend: максимум разности логитов по окнам 512 токенов с перекрытием 64; порог Guardrails 3.566. |

Ollama получает до 22 CPU и 40 GiB RAM, `OLLAMA_MAX_LOADED_MODELS=3`, параллелизм — 1. `native-models` получает до 6 CPU и 8 GiB, Horizon ONNX — до 2 CPU и 4 GiB; оставшиеся ресурсы используются приложением и хранилищами. Это стартовые лимиты, а не измеренная производительность. На CPU генерация 4B и VLM 4B могут занимать минуты; в подготовленном `.env` увеличены таймауты.

`OLLAMA_GATEWAY_KEEP_ALIVE=-1` сохраняет модели в памяти при наличии ресурсов. Адаптер передаёт числовое значение как JSON-число; строка `"-1"` не является корректной Go duration и раньше вызывала HTTP 400 на пробе embedding. Веса Ollama сохраняются в `ollama_data`. Native SDK и веса изолированы в отдельном образе и томе `native_model_data`; `COMPOSE_PROFILES=native` включает оба CPU-сервиса при обычном `up`. Все репозитории выбранного набора публичны, HF-токен не требуется. Веса скачиваются при запуске, не при сборке; только Ollama-файлы занимают примерно 8 GB, дополнительно нужны CPU-веса, образы и место для документов.

В `.env` включены `RAG_TEXT_ONLY=false`, `RAG_USE_RERANKER=true`, три provider-модуля, согласованы профили Core/Ingestion и новые коллекции `document_chunks_bge_m3_v1` / `topic_questions_bge_m3_v1`. Бюджет Core учитывает русский системный промпт из `features`; `validate` проверяет наличие места для фрагментов и соответствие контексту Ollama. [Native CPU: нормализация и ограничения](../model-gateway/native-cpu.md). Существующий ONNX-сервис детектора переиспользуется без изменения модели, окон и нормализации. Корневой Compose собирает его через `deploy/detector/Dockerfile`: при запуске скачиваются и сверяются закреплённые файлы в `detector_model_data`. Локальный Dockerfile с весами в образе сохранён.

Смена embedding требует нового индекса даже при той же размерности. После неудачного первого старта старый профиль мог уже сохраниться в пустой БД. `ALLOW_EMPTY_EMBEDDING_PROFILE_SWITCH=true` разрешает обновить метаданные только при нулевом поколении и отсутствии документов, редакций и вопросов. Остановите `core`/`worker` перед переключением. Старые коллекции не удаляются; при наличии данных подготовка завершается `embedding_profile_change_requires_reindex`, и требуется согласованная переиндексация. Не очищайте тома ради обхода проверки.

`MODEL_DOWNLOAD_TIMEOUT_S` ограничивает загрузку одной модели. `RAG_STARTUP_TIMEOUT_S` и `MODEL_GATEWAY_STARTUP_TIMEOUT_S` учитывают CPU-прогрев. Веса и модели не загружаются при `docker build`.

## Доступ

| Сервис | Доступ |
| --- | --- |
| nginx | Публичные TCP 80 и 443. |
| API и загрузка | Через HTTPS nginx; прямых портов на хосте нет. |
| PostgreSQL, RabbitMQ, Redis, Qdrant, S3, Ollama, native-models, injection-detector, Loki, Alloy | Только сеть Compose. |
| Grafana | `127.0.0.1:3300` сервера через SSH-туннель. |

```bash
ssh -N -L 3300:127.0.0.1:3300 user@SERVER_IP
```

Откройте `http://127.0.0.1:3300` на своём компьютере. Credentials — `GRAFANA_USER` / `GRAFANA_PASSWORD`. В папке RAG доступны RAG Overview, RAG Pipeline Performance, Messaging & Reliability, Ingestion & Document Lifecycle. Данные появляются из событий приложения. Loki хранит историю семь дней; логи контейнеров ротируются по 10 MiB, до трёх файлов.

Админка бота: `https://PUBLIC_HOST/admin`, credentials — `ADMIN_LOGIN` / `ADMIN_PASSWORD`. При `RAG_BOOTSTRAP_USERNAMES=[]` добавьте username сотрудника через эту страницу перед первым вопросом. Непустой список задаёт первоначальный допуск и повторно разрешает указанных сотрудников при каждом `prepare`; для управления только из админки оставьте список пустым.

Все страницы и API используют один HTTPS origin. CORS не добавлен. Для будущего Mini App на другом origin настройка CORS и серверная проверка Origin согласуются отдельно.

## Документы

```bash
curl --request POST 'https://SERVER_IP/admin/ingestion/upload?filename=manual.md&access_level=employees' \
  --header "Authorization: Bearer $RAG_INGESTION_TOKEN" \
  --header 'Content-Type: text/markdown' \
  --data-binary @manual.md
```

Токен в примере берётся из окружения терминала оператора. Не выводите `.env` в логи и не передавайте браузеру. HTTP 202 означает приём задания, не завершение индексации. Проверяйте логи `worker` и состояние документа перед вопросом. Не повторяйте загрузку при неизвестном исходе без проверки регистрации.

nginx передаёт тело потоком без сохранения исходника в proxy temp-файл. `INGESTION_PROXY_TIMEOUT_S` должен превышать `RAG_INGESTION_UPLOAD_TIMEOUT_S`; у подготовленного окружения это учтено. OCR/Vision используются конвертером документов, OCR также обрабатывает фотографии вопросов.

## Обновление и данные

Новый сервер получает пустой корпус. Git не переносит локальные базы и документы. Для прежнего корпуса восстановите согласованные PostgreSQL, обе коллекции Qdrant, S3 и spool по [процедуре резервного копирования](../faq/production-update.md). Имя проекта сохранено `rag-hackathon`; его смена меняет стандартные имена томов.

Для обновления с остановленной записью:

```bash
docker compose stop core worker
git pull --ff-only
docker compose up -d --build
docker compose ps -a
docker compose logs --tail 100 migrate prepare models-init nginx webhook
```

Перед изменением схемы/индекса сделайте согласованную резервную копию. Возврат к старому образу после несовместимой миграции не является безопасным откатом. Проверяйте завершение подготовительных задач новой версии. После смены `.env` используйте `up`: `restart` не обновляет окружение.

Тома сохраняются при пересоздании контейнеров. `docker compose down -v` удаляет данные и сертификаты и не является обновлением. Серверные команды не требуют менеджера контейнеров; не запускайте менеджер и корневой Compose одновременно над одним проектом.

## Проверка

- `nginx`, `core`, `worker` здоровы; подготовка завершилась с кодом 0.
- `https://PUBLIC_HOST/health/live` и `/health/ready` доступны извне с проверкой сертификата.
- Сотрудник допущен, документ проиндексирован, вопрос завершается ожидаемым ответом/эскалацией.
- Работают ссылки на источник, тикет и административные действия с того же origin.
- Grafana получает события; публичны только ожидаемые порты.
- После перезагрузки сервер автоматически поднимает контейнеры, данные сохранены.
- Проверьте продление: `docker compose exec nginx certbot renew --dry-run`. Эта проверка не заменяет реальный сертификат.

Проверки кода развёртывания без настоящих моделей и Telegram:

```bash
python -m unittest discover -s deploy/tests -v
```

Требуются установленное приложение и Docker Compose CLI. Демон нужен для сборки и интеграции. Для тестовых стендов не используйте настоящий `.env` или токен.


## Mini App на том же HTTPS origin

Склонируйте `https://github.com/lUniverseGameplay/docs-bots_TG_mini_app.git`
рядом с docs-bots. Override `compose.mini-app.yaml` собирает его frontend/backend;
`MINI_APP_DIR` позволяет задать другой путь. В серверный `.env` на Linux добавьте:

```dotenv
COMPOSE_FILE=compose.yaml:compose.mini-app.yaml
MINI_APP_ENABLED=true
MINI_APP_URL=https://176.53.246.245
```

Замените IP своим `PUBLIC_HOST`. `MINI_APP_ADMIN_IDS` содержит только явно назначенные
числовые Telegram ID; пустое значение не назначает администратора. После резервной
копии PostgreSQL выполните обычный `docker compose up -d --build`: миграции
включены в существующую цепочку запуска. При обновлении обоих репозиториев
выполните `git pull` в каждом перед сборкой. На Windows разделитель COMPOSE_FILE — `;`.

Публичный nginx отправляет `/` во frontend, `/api/v1/` и `/sources/` в backend
Mini App, а health, старую админку и тикеты — в core. Приём документов сохраняет
отдельный маршрут worker. Backend обращается к `http://rag-api:8000` по внутренней
сети `mini`, не получает секреты бота и не подключается к сети хранилищ.
Порты Mini App на хост не публикуются; CORS не требуется. Без override прежняя
маршрутизация сохраняется. Существующие сертификаты и их продление используются
для того же IP или домена.

Через Bot API задайте Menu Button типа `web_app` на `MINI_APP_URL` для этого же
бота. Main Mini App в профиле настраивается отдельно в BotFather. Проверяйте вход
из Telegram: обычный браузер без подписанного initData не имеет доступа к API.
Публичный `/api/v1/me` без авторизации должен возвращать 401, `/health/ready` — 200.
