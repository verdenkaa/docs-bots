---
title: GigaChat для ответов пользователям
sidebar_position: 8
---

:::warning Проверка рабочего профиля
HTTP-контракт проверен тестами, а JSON и цитаты — короткой реальной пробой
GigaChat-2-Max на трёх синтетических вопросах. Качество на рабочем корпусе
и полное время ответа бота нужно проверить на целевом сервере.
:::

`RAG_USE_GIGACHAT=true` заменяет только binding `generation` процесса `questions`.
Answer Generator продолжает вызывать Model Gateway и проверять JSON, ссылки на
фрагменты и изображения. Публичные DTO и сигнатуры не меняются.
Worker использует исходный `generation` из `RAG_MODEL_BINDINGS`: вопросы для
индексации, OCR, vision, embedding, детектор и reranker не переключаются.

## Настройки

| Переменная | Значение |
| --- | --- |
| `RAG_USE_GIGACHAT` | `false` по умолчанию. `true` включает API только для ответов. |
| `GIGACHAT_MODEL` | Обязательный при включении идентификатор доступной вам модели. |
| `GIGACHAT_CREDENTIALS` | Ключ авторизации для обмена на access token; только в секретном `.env`. |
| `GIGACHAT_SCOPE` | `GIGACHAT_API_PERS` по умолчанию; для соответствующих аккаунтов `GIGACHAT_API_B2B` / `GIGACHAT_API_CORP`. |
| `GIGACHAT_ENDPOINT` | `https://api.giga.chat/v1`; адаптер добавляет `/chat/completions`. |
| `GIGACHAT_AUTH_URL` | `https://ngw.devices.sberbank.ru:9443/api/v2/oauth`. |
| `GIGACHAT_CA_BUNDLE` | Необязательный путь к дополнительному CA PEM внутри контейнера. |
| `GIGACHAT_VERIFY_SSL_CERTS` | `true` по умолчанию. `false` передаёт HTTPX `verify=False` только для OAuth/API клиента GigaChat; CA PEM не требуется и не читается. |
| `GIGACHAT_MAX_CONCURRENCY` | `1` по умолчанию; согласуйте повышение с квотой API. |

URL требуют HTTPS без credentials, query и fragment. При `false` настройки
GigaChat, ключ и дополнительный CA не требуются. Приложение не перечитывает
окружение работающего процесса.

## Включение и откат без пересборки

Первый раз соберите и разверните новый образ с адаптером обычным способом.
Для API нужны исходящие HTTPS-соединения к API и OAuth (в том числе TCP 9443).
Вопрос и отобранные фрагменты документации передаются внешнему API.

При `GIGACHAT_VERIFY_SSL_CERTS=true` установите необходимые доверенные сертификаты по
[официальной инструкции](https://developers.sber.ru/docs/ru/gigachat/certificates).
В корневом Compose каталог хоста `deploy/certs/` монтируется в `/etc/rag/certs/`.
Например, после помещения туда публичного корневого CA PEM:

~~~dotenv
RAG_USE_GIGACHAT=true
GIGACHAT_MODEL=GigaChat-2-Max
GIGACHAT_CREDENTIALS=<ключ авторизации из вашего аккаунта>
GIGACHAT_SCOPE=GIGACHAT_API_PERS
GIGACHAT_CA_BUNDLE=/etc/rag/certs/russian_trusted_root_ca_pem.crt
~~~

Выберите модель и scope своего аккаунта.

Для проверки без CA PEM задайте `GIGACHAT_VERIFY_SSL_CERTS=false` и
не задавайте `GIGACHAT_CA_BUNDLE`. Это соответствует `verify_ssl_certs=False`
в SDK GigaChat; в HTTPX параметр называется `verify=False`.

Команды переключения уже установленного образа из корня проекта:

~~~bash
docker compose run --rm --no-deps validate
docker compose stop core
docker compose up -d --no-deps --no-build --force-recreate core
docker compose logs --tail 100 core
~~~

Остановка перед пересозданием даёт старым обработчикам время завершиться.
Это кратковременная остановка Core, а не горячее переключение отдельного запроса.
`restart` не обновляет env контейнера. Worker и хранилища пересоздавать не нужно.
Образ `DEPLOY_APP_IMAGE` (по умолчанию `rag-bot:server`) должен уже содержать
эту интеграцию. Команда проверки использует тот же образ.

Для отката задайте `RAG_USE_GIGACHAT=false` и повторите те же команды.
Исходный `RAG_MODEL_BINDINGS` менять не требуется; локальная модель остаётся
подготовленной для Worker и возврата к локальным ответам.

## Профиль, кэш и диагностика

GigaChat получает отдельный `profile_id` с отпечатком модели, endpoint, scope
и версии нормализации ответа. Он виден в обычных логах `model_gateway` вместе
с токенами, `wait_ms` и `inference_ms`. Тексты и ключи адаптер не логирует.
К префиксу Exact Cache автоматически добавляется профиль GigaChat: локальные
и API-ответы не смешиваются. При откате возвращается исходное пространство кэша;
ранее зафиксированные ответы и уже созданные доставки не переписываются.

Идентификатор API-модели записывается в `model_revision`, но не является
SHA-256 весов: поставщик может обновить сервис под тем же именем. При такой
смене задайте новый namespace через `RAG_CACHE_PREFIX` для повторной оценки.
Embedding и коллекции Qdrant не меняются, переиндексация не нужна.

`check-models --role generation` при включённом флаге проверяет GigaChat.
Полный `models-init` проверяет и локальные модели, и отдельный API-профиль.

## Адаптер и ошибки

Адаптер `app/rag/model_gateway/gigachat.py` регистрируется Gateway при явном
выборе binding с provider `gigachat`. Импорт не выполняет сетевых запросов.

### <span className="operation-badge operation-badge--call">CALL</span> `register_providers`

~~~python
def register_providers() -> None: ...
~~~

Hook регистрирует factory `ModelAdapter`. При старте создаётся HTTPX-клиент с
настроенной через `GIGACHAT_VERIFY_SSL_CERTS` проверкой TLS, без системного proxy
и redirect. Остальные HTTP-клиенты приложения эта настройка не меняет.
Проба выполняется через Gateway.
OAuth access token переиспользуется и обновляется заранее под асинхронным lock.
После явного HTTP 401 разрешено одно обновление токена и повтор запроса.
Таймаут/обрыв соединения не повторяет POST генерации внутри адаптера.

`generate` передаёт сообщения, `max_output_tokens` как `max_tokens` и температуру.
`response_format` с `type=json_schema` и корневым `type=object` явно включает
JSON-объект для обоих исходов Answer Generator. Бизнес-схему, обязательные поля
и происхождение цитат продолжает проверять Answer Generator. Для readiness
используется отдельная короткая JSON-проба с лимитом 64 токена.
Принимается один текстовый assistant-ответ без function/tool calls. Завершения
`stop`, `length`, `blacklist`/`content_filter` нормализуются в
`completed`, `length`, `filtered`. Доступные счётчики нормализуются в TokenUsage.
JSON и цитаты по-прежнему проверяет Answer Generator; схема ответа не меняется.

HTTP 408 даёт `model_timeout`, 429 — `model_overloaded`, 5xx —
`model_unavailable`; это временные ошибки существующей политики Gateway/Core.
401/402/403/404 после допустимого обновления авторизации —
`model_configuration_error`, остальные 4xx — `model_invalid_input`.
Повреждённый/слишком большой ответ — `model_invalid_response`.
Весь вызов, включая OAuth и ожидание lock, ограничен одним deadline.
При ошибке API скрытого fallback на Ollama нет: откат выполняется флагом.

Протокол: [авторизация](https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/gigachat-api),
[генерация](https://developers.sber.ru/docs/ru/gigachat/api/reference/rest/post-chat).
