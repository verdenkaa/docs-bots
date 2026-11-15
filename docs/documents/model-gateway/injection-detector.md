---
title: Сервис детектора инъекций
sidebar_position: 7
---

:::warning
Сервис и адаптер покрыты тестами без контейнерного движка. Сборка образа и запуск в Compose на общем стенде ещё не проверены; задержку и память на сервере стенда нужно замерить отдельно.
:::

Детектор prompt injection работает отдельным контейнером рядом с приложением. В локальном стенде `detector` использует Debian, поскольку для Alpine-образов приложения нет сборок ONNX Runtime и PyTorch. В [серверном запуске](../infrastructure/server-deployment.md) приложение также использует Debian, а изоляция SDK сохраняется. Для Model Gateway это [собственный сервер инференса](./lifecycle.md): binding с `execution="remote"`. Guardrails по-прежнему вызывает только `classify_injection`.

| Что | Где |
| --- | --- |
| HTTP-сервис и образ | `app/injection_detector/`: `server.py`, `model_files.py`, `Dockerfile`, `requirements.txt` |
| Адаптер Model Gateway | `app/rag/model_gateway/injection_detector.py`, provider `injection_detector` |
| Сервис Compose | `detector` в группе `models`, [запуск стенда](../faq/container-manager.md) |
| Пороги и качество | [Детектор и проверка качества](../guardrails/detector.md#результаты-подбора) |

## Модель

`Horizon-Labs/prompt-injection-guard-base`, ревизия `fc071c5754f8cf2edce9dc1d7fd3513ef9287007`, файл `onnx/model.onnx` (1,2 ГБ, полная точность). Исполняется на CPU через ONNX Runtime; PyTorch не нужен. Зависимости образа: `onnxruntime`, `tokenizers`, `numpy`.

Веса и токенизатор входят в образ. При сборке `model_files.py` скачивает закреплённые файлы и сверяет размер и SHA-256; при запуске сервис сверяет их ещё раз и без совпадения не стартует. Сеть при запуске не нужна. Первая сборка скачивает 1,2 ГБ; слой с весами зависит только от `model_files.py` и при правке сервера не пересобирается.

Это поведение локального `app/injection_detector/Dockerfile`. Корневой серверный Compose использует `deploy/detector/Dockerfile`: тот же downloader и HTTP-код, но загрузка происходит при старте в постоянный том `detector_model_data`. Сервис называется `injection-detector`, endpoint — `http://injection-detector:8080`, порт на хост не публикуется. Профиль `injection-horizon-base-v1` и порог 3.566 сохраняются. Новые веса при сборке серверного образа не скачиваются.

Сервис возвращает оценку — разность логитов «инъекция − безопасно»: больше — опаснее, это не вероятность. Текст длиннее 512 токенов оценивается перекрывающимися окнами (перекрытие 64 токена), берётся максимум. Порогов в сервисе нет. Оценки сервиса сверены с оценками PyTorch, на которых подбирались пороги: на 937 запросах наибольшее расхождение 0,00008, ни одно решение не изменилось. Квантованный файл `model_quantized.onnx` не используется: его оценки отличаются, пороги пришлось бы подбирать заново.

## HTTP-интерфейс сервиса

Внутренний интерфейс сети Compose, порт 8080. Аутентификации нет: порт не публикуется наружу, локально — только на loopback.

| Запрос | Ответ 200 | Ошибки |
| --- | --- | --- |
| `GET /health/ready` | `{"status": "ready", "model_id": "...", "model_revision": "..."}`. Сервер начинает слушать только после загрузки модели. | — |
| `POST /classify` с телом `{"text": "..."}` | `{"score": 4.5, "model_id": "...", "model_revision": "..."}` | 400 — не JSON, нет непустой строки `text`; 411/413 — нет длины или тело больше `DETECTOR_MAX_BODY_BYTES`; 500 — сбой inference. |

Тексты вопросов сервис не журналирует.

| Переменная сервиса | По умолчанию | Назначение |
| --- | --- | --- |
| DETECTOR_MODEL_DIR | /opt/detector/model | Каталог с файлами модели. |
| DETECTOR_PORT | 8080 | Порт внутри контейнера. |
| DETECTOR_THREADS | 0 | Число потоков ONNX Runtime на один вызов; 0 — выбор библиотеки. |
| DETECTOR_MAX_BODY_BYTES | 262144 | Предел тела запроса. |

## Подключение к Model Gateway

В корневом `.env` стенда:

~~~text
MODEL_GATEWAY_PROVIDER_MODULES='["rag.model_gateway.injection_detector"]'
RAG_MODEL_BINDINGS='{"injection": {"profile_id": "injection-horizon-base-v1", "execution": "remote", "provider": "injection_detector", "model_id": "Horizon-Labs/prompt-injection-guard-base", "model_revision": "fc071c5754f8cf2edce9dc1d7fd3513ef9287007", "endpoint": "http://detector:8080", "credential_env": null, "max_concurrency": 4, "embedding_dimensions": null}}'
RAG_GUARDRAILS_INJECTION_SCORE_THRESHOLDS='{"injection-horizon-base-v1": 3.566}'
~~~

Остальные роли и модули провайдеров добавляются в те же `RAG_MODEL_BINDINGS` и `MODEL_GATEWAY_PROVIDER_MODULES`: например, `["rag.model_gateway.ollama", "rag.model_gateway.injection_detector"]`. При запуске `core` из IDE endpoint — `http://127.0.0.1:8090` (порт публикуется в режиме `--local`).

Адаптер возвращает `InjectionResult` с `label=None` и `score`: при наличии метки Guardrails не применил бы пороги. Пороги привязаны к `profile_id`; при смене модели, ревизии, файла весов или параметров окон задавайте новый `profile_id` и подбирайте пороги заново.

### <span className="operation-badge operation-badge--call">CALL</span> `register_providers`

~~~python
def register_providers() -> None: ...
~~~

Технический синхронный hook регистрирует фабрику под именем `injection_detector`. Вызывается при явном start_models через MODEL_GATEWAY_PROVIDER_MODULES; сам по себе соединений не открывает.

## Правила и ошибки адаптера

- Binding принимается только для роли `injection`, с `execution="remote"` и без `credential_env`: у сервиса нет аутентификации. Иначе — `model_configuration_error`.
- При запуске адаптер запрашивает `/health/ready`. Каждый ответ сервиса содержит `model_id` и `model_revision`; несовпадение с binding даёт `model_profile_mismatch` — другая модель за тем же адресом не ответит под прежним профилем.
- Нечисловая, бесконечная или отсутствующая оценка — `model_invalid_response`, а не `benign`.
- Ответ 5xx и сетевой сбой — `model_unavailable`, таймаут — `model_timeout` (retryable). Guardrails переводит их в `detector_unavailable`; вопрос дальше не идёт.
- POST автоматически не повторяется; действует общая [защита повторов Gateway](./providers.md#повторы).

## Запуск без контейнера

Для разработки сервис можно запустить в отдельном окружении Python 3.12 из `app/injection_detector/`:

~~~sh
python -m pip install -r requirements.txt
DETECTOR_MODEL_DIR=./model python model_files.py
DETECTOR_MODEL_DIR=./model DETECTOR_PORT=8090 python server.py
~~~

Каталог `model/` в репозиторий не добавляется.

## Ресурсы

Один контейнер держит одну копию модели: около 1,3 ГБ файла весов в памяти плюс рабочая память ONNX Runtime. На машине разработчика (CPU) ONNX Runtime отвечал за 40 мс на вопрос по медиане и 88 мс на 95-м процентиле; на сервере стенда задержку нужно замерить. `max_concurrency` в binding ограничивает число одновременных запросов от одного процесса `core`.
