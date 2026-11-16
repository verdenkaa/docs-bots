---
title: "Telegram Mini App: HTTP и эксплуатация"
sidebar_position: 11
---

:::warning Граница проверки
Сквозной запуск с реальным Telegram и рабочими моделями требует HTTPS-доменов,
настройки BotFather и доступной инфраструктуры. Проверки с подменённым модельным
ответом не подтверждают качество RAG. Входящие изображения пока отключены.
:::

## Канал и совместимость

Frontend и backend находятся в отдельном репозитории `docs-bots_TG_mini_app`.
Frontend → HTTPS backend Mini App → HTTP RAG API по общей Docker-сети.
Backend не читает БД, брокер или модели; RAG API остаётся владельцем прав,
вопросов, результатов, тикетов и административных операций.
[Новые DTO](./mini-app-contracts.md).

`QuestionInput` остаётся прежним: Mini App регистрирует только `TextQuestion`.
Транзакция сохраняет `rag_requests`, `web_requests` и outbox `rag.questions`.
Прежние `RequestRef`, закрепление поколения, OCR Telegram-входов, Guardrails,
Exact Cache, Searcher, Model Gateway и retry/recovery не меняют своих контрактов.
Один активный вопрос проверяется по Telegram ID сразу для обоих каналов.

При завершении веб-вопроса Runtime сохраняет исходный `QuestionOutcome`,
`web_requests.completed_at` и `state=finished` в одной транзакции. Активный слот
освобождается без чтения результата. `rag.results`, Telegram-доставки и фиктивные
квитанции для веб-вопроса не создаются. При автоматической эскалации сохраняется
`SupportCase(creating)`, который открывает прежняя роль `maintenance`.
Роли `results`, `maintenance`, `recovery`, `relay`, `questions` сохраняются.

Кнопка «Ответ не помог» сразу создаёт тикет; повтор возвращает то же обращение.
Кнопка «Нужна помощь» создаёт устойчивое ожидание 10 минут. Повтор
возвращает то же обращение и не сбрасывает срок. После срока доступно
подтверждение либо отказ; `maintenance` создаёт подтверждённый тикет. Для веба
новая стадия читается через HTTP без Telegram-уведомления. Снимок тикета содержит
сохранённый Markdown веб-ответа; у прежнего Telegram-канала сохраняются фактически
доставленные тексты из реальных квитанций.

## Доступ и ошибки

Все новые ручки, включая запросы между backend и RAG API, требуют
`Authorization: tma <исходный initData>`. RAG API проверяет HMAC-SHA256 по токену
бота, уникальность ключей, `auth_date`, Telegram ID и username. По умолчанию
срок 3600 секунд, допустимое опережение часов 30 секунд. При истечении нужно
переоткрыть Mini App. Токен бота хранится только в docs-bots; initData не попадает
в URL, хранилище frontend или журналы приложения.

Администраторы задаются списком Telegram ID в `MINI_APP_ADMIN_IDS`. Допуск по
username сам по себе не даёт роль admin. Первого администратора можно назначить
до заполнения допуска; управление допуском доступно ему без разрешения вопросов.
Для новых вопросов администратору также нужен допуск username.

Результат читает только владелец веб-вопроса. Изображение доступно, только если
его ID присутствует в сохранённом ответе этого пользователя. Пользовательские
тикеты фильтруются по владельцу, административные маршруты отдельно проверяют
роль. Отключение допуска не отменяет принятые вопросы и чтение своих результатов.
Старые публичные `/tickets/{case_id}` остаются публичными; это прежний явно
сохранённый контракт, а не защищённая история Mini App.

Общие ошибки: 401 — подпись/срок; 403 — роль/допуск; 404 — отсутствующая или чужая
запись; 409 — конфликт состояния/ключа; 413 — размер; 422 — вход; 429 — лимит;
503 — временная недоступность. У старых бизнес-исключений тело может быть пустым.
Лимит нового API: 120 запросов в минуту на проверенного пользователя на процесс,
429 содержит `Retry-After`. При нескольких HTTP workers лимит суммируется;
общий эксплуатационный лимит настраивается на reverse proxy. Сообщения тикета
имеют дополнительный устойчивый лимит 20 в минуту на тикет и роль.

## Ручки

Backend Mini App повторяет перечисленные пути и методы. Запрещены произвольные
upstream URL и маршруты вне списка. В примерах базовый префикс `/api/v1`.
Для изменяющих запросов `Content-Type: application/json`, тело не более 32 KiB.

### <span className="operation-badge operation-badge--get">GET</span> `/api/v1/me`

Проверенный пользователь; 200: `user_id`, `username`, `role`, `admitted`,
`active_request_id` своего незавершённого веб-вопроса, `capabilities`
(`text_questions`, `image_uploads=false`, `tickets`, `admin`). Чтение безопасно
повторять. Нет списка прошлых RAG-вопросов и серверного контекста диалога.

### <span className="operation-badge operation-badge--post">POST</span> `/api/v1/questions`

Допущенный пользователь. Заголовок `Idempotency-Key`: 8–128 символов
`A-Za-z0-9._:-`. Вход: `{"text":"Как выполнить настройку?"}`.
202 после commit: `{"request_id":123}`. Ключ ограничен владельцем и хранится
устойчиво; повтор того же тела возвращает тот же ID, другое тело — 409.
Пробелы по краям нормализуются. При одном активном вопросе новый получает
409 `active_question_exists`; при общей перегрузке — 503 с `Retry-After`.
Безопасный повтор принятого запроса допускается после отключения username.
POST не ждёт моделей. Ключ и тело при неоднозначной сетевой ошибке не меняются.

### <span className="operation-badge operation-badge--get">GET</span> `/api/v1/questions/{request_id}`

Только владелец; положительный bigint. 200: `request_id`, `state`, `text`,
`received_at`, `outcome` (null до завершения, затем прежний `QuestionOutcome`),
`support` (null либо `SupportCase`). Polling frontend каждые 3 секунды с backoff
и прекращением при готовности. Чтение не вызывает генерацию. Текущий ID хранится
локально под проверенным Telegram ID; после открытия восстанавливается результат.

### <span className="operation-badge operation-badge--post">POST</span> `/api/v1/uploads`

Авторизованный пользователь. 501 `image_uploads_disabled`. Тело не читается
прикладным обработчиком и не сохраняется. Frontend этот запрос не выполняет.

### <span className="operation-badge operation-badge--get">GET</span> `/api/v1/images/{image_id}`

UUID из собственного сохранённого ответа. После проверки владельца читает
изображение через существующий `s3.get_image`. 200: байты и проверенный MIME,
`Cache-Control: no-store`. Неизвестный/чужой UUID — 404. Frontend использует
авторизованный fetch и освобождаемые Blob URL, без секретов в query.

### <span className="operation-badge operation-badge--post">POST</span> `/api/v1/questions/{request_id}/support`

Владелец готового ответа, действующий допуск. Вход:
`{"reason":"dissatisfied"}` либо `{"reason":"human_request"}`.
200: для `dissatisfied` — открытый тикет; для `human_request` — `SupportCase(waiting)` с `check_at` через 10 минут. Повтор возвращает то же
обращение. Для неотвеченного вопроса — 409; автоматическая эскалация уже создаёт
обращение без этого запроса. Кнопки интерфейса не меняют исходный ответ Core.

### <span className="operation-badge operation-badge--get">GET</span> `/api/v1/support/{case_id}`

Владелец; 200 `SupportCase`. Чтение может перевести истёкшее веб-ожидание в
`awaiting_confirmation`; таймер не сбрасывается. Приложение опрашивает активное
обращение каждые 5 секунд. Открытый тикет доступен через ручки сообщений.

### <span className="operation-badge operation-badge--get">GET</span> `/api/v1/support`

Только собственные незавершённые веб-обращения waiting/awaiting_confirmation/creating.
Query: `limit=30` (1–100), `before_request_id` — исключающий положительный bigint.
200: `{items:[SupportCase],next_cursor}`. Это список поддержки, не история вопросов.
Он восстанавливает ожидания после нового вопроса, закрытия приложения и перехода
на другое устройство. Frontend опрашивает страницу раз в 10 секунд, предыдущие
страницы доступны отдельно. Стадии прежних Telegram-обращений обрабатывают старые
callbacks и maintenance; новая ручка их не подменяет.

### <span className="operation-badge operation-badge--post">POST</span> `/api/v1/support/{case_id}/confirm`

Владелец и допуск; без тела. В `awaiting_confirmation` сохраняет `creating`;
200 `SupportCase`. Прежняя maintenance открывает тикет. Повтор безопасен.
Подтверждение до срока или после отмены — 409.

### <span className="operation-badge operation-badge--post">POST</span> `/api/v1/support/{case_id}/decline`

Владелец и допуск; без тела. Из `awaiting_confirmation` сохраняет `cancelled`;
200 `SupportCase`. Повтор отказа безопасен; преждевременный отказ — 409.

### <span className="operation-badge operation-badge--get">GET</span> `/api/v1/tickets`

Только собственные open/closed. Query: `limit=30` (1–100), необязательный
`before_request_id` (положительный bigint, исключающий курсор).
200: `{"items":[SupportCase],"next_cursor":123}`; null означает конец списка.
Сортировка по request_id убывает. Чтение безопасно повторять.

### <span className="operation-badge operation-badge--get">GET</span> `/api/v1/tickets/{case_id}/messages`

Владелец существующего тикета. Query: `limit=50` (1–100),
`before_message_id` — исключающий положительный bigint.
200: прежний `TicketView`: case, history, messages, next_cursor. Страница
содержит последние сообщения в порядке возрастания; доступны предыдущие страницы.

### <span className="operation-badge operation-badge--post">POST</span> `/api/v1/tickets/{case_id}/messages`

Владелец открытого тикета. Вход — прежний `TicketMessageInput`:
`{"client_message_id":"UUID","text":"Уточнение"}`, текст 1–4096 символов.
200 `TicketMessage`; автор visitor определяется сервером. Повтор UUID и текста
возвращает прежнее сообщение, другое содержимое — 409. Закрытый тикет — 409.
Успешный повтор старого сообщения допустим и после закрытия.

### Административные маршруты

Каждый маршрут проверяет Telegram-роль admin независимо от frontend.
Старый `Principal` и Basic credentials здесь не используются.

| Метод и путь | Вход и результат | Повтор |
| --- | --- | --- |
| GET `/api/v1/admin/tickets` | Та же пагинация, все open/closed тикеты | Безопасен |
| GET `/api/v1/admin/tickets/{case_id}/messages` | Те же query и `TicketView`, любой существующий тикет | Безопасен |
| POST `/api/v1/admin/tickets/{case_id}/messages` | `TicketMessageInput` → `TicketMessage`, автор operator, login `telegram:ID` | UUID + тот же текст и автор |
| POST `/api/v1/admin/tickets/{case_id}/close` | Без тела → `SupportCase(closed)` | Безопасен |
| GET `/api/v1/admin/users` | `limit=50` (1–100), `after` username до 32 символов; `{items:[AccessRecord],next_cursor}` | Безопасен |
| PUT `/api/v1/admin/users/{username}` | username 1–32, JSON `{"enabled":true}` → `AccessRecord` | Безопасен |
| GET `/api/v1/admin/metrics` | Обязательные aware datetime `from_at`, `to_at`, начало меньше конца; `MetricsSnapshot` + `web_result_duration_ms` | Безопасен |

`reply_duration_ms` сохраняет прежний смысл подтверждённых Telegram-доставок.
Для Mini App отдельно измеряется доступность **всех** готовых веб-результатов
(ответ, уведомление, эскалация), без утверждения, что пользователь их прочитал.
Новая ручка метрик учитывает веб-реакцию «Ответ не помог» через причину обращения.
Старые метрики и публичные контракты не меняют свою форму и определения.

## Telegram-вход и интерфейс

Telegram использует только long polling. Внешняя регистрация webhook удалена. Poller передаёт updates по Docker-сети на `/internal/telegram/updates` с `X-Telegram-Intake-Secret`; публичный nginx закрывает эту ручку. `/start`, `/help` и обычные личные сообщения получают приглашение открыть Mini App через notice/Outbox. Прочие slash-команды молча пропускаются. Такое поведение не зависит от MINI_APP_ENABLED; флаг управляет доступностью `/api/v1/*`. Старые callback и ранее созданные доставки сохраняют прежний путь.

Отдельный backend Mini App обращается напрямую к `/api/v1/*`; этот контракт не изменён. Авторизация initData использует TELEGRAM_BOT_TOKEN и не требует intake-secret. RAG_PUBLIC_URL остаётся публичным адресом для ссылок. Старые env-имена URL/secret читаются как совместимые алиасы. Изменения отдельного репозитория Mini App выполняет его владелец.

Frontend подключает официальный Telegram SDK, использует ready/expand/themeChanged.
Пользователь видит чат и свои тикеты. Администратор дополнительно видит все
тикеты, допуск и метрики. Markdown: react-markdown + remark-gfm, без HTML/MDX.
Плейсхолдеры `image://UUID` обрабатываются в AST, проверяются по result.images;
встроенные Markdown-изображения не загружаются. Галерея находится ниже текста
ответа, сохраняет описания и позволяет увеличить изображение. RAG API передаёт
source_url внутри result.sources: клиент использует этот HTTP(S) URL напрямую,
а при None — существующую публичную HTML-заглушку `/sources/{document_id}`.
Постоянного хранилища исходных Markdown в RAG-приложении нет; URL задаёт
отправитель документа, сервер по нему ничего не скачивает.

## Развёртывание

1. Сделать резервную копию PostgreSQL. Пересобрать все процессы docs-bots с новым
   кодом. Применить `python -m rag.runtime migrate` до `003_mini_app`, сохранив
   очередь и незавершённые работы. Не включать веб-приём при старых workers.
2. В корневом `.env` docs-bots задать `MINI_APP_ENABLED=true`,
   `MINI_APP_URL=https://mini.example.org`, `MINI_APP_ADMIN_IDS=123456789`,
   `MINI_APP_AUTH_MAX_AGE_S=3600`. Сохранить прежний токен, настроить TELEGRAM_INTAKE_SECRET и RAG_PUBLIC_URL,
   ADMIN_LOGIN/PASSWORD и модельные/инфраструктурные настройки.
3. Присоединить RAG API и Mini App к одной сети; задать RAG API DNS alias `rag-api`.
   В отдельном репозитории настроить `.env` по `.env.example`, затем
   `docker compose up -d --build`. Его backend использует `http://rag-api:8000`.
4. Настроить HTTPS reverse proxy по примеру отдельного репозитория. Frontend и
   backend имеют один browser origin. Публичный RAG API сохраняет свой домен и
   прежние пути; новые ручки доступны только с проверенным initData.
5. В BotFather для этого же бота задать Main Mini App и Menu Button на HTTPS URL
   frontend. Проверить открытие меню, /start и ранее отправленные callbacks через polling.
6. Первый admin открывает приложение, добавляет разрешённые username через
   «Допуск»; затем выполнить текстовый вопрос, повтор POST, закрытие/открытие,
   ручной и автоматический тикеты, ответ оператора и закрытие.

Миграция совместима со старыми строками: channel по умолчанию telegram,
Telegram chat/update сохраняются. Для web оба поля null. Откат DDL запрещён при
наличии web-строк: восстановить согласованный backup, не подделывать идентичности
и не удалять историю автоматически. Простой откат функционала — выключить
MINI_APP_ENABLED, сохранив новый Runtime и схему.

Проверки: приложение `python -m unittest discover -s tests -v` из app;
документация `npm test`, `npm run build` из docs. Новый PostgreSQL-набор:
`RUN_MINI_APP_INTEGRATION=1` при доступном тестовом PostgreSQL. Он создаёт и
удаляет только отдельную случайную схему. Инструкции Mini App и его проверки
находятся в README отдельного репозитория.

[Официальная документация Telegram Mini Apps](https://core.telegram.org/bots/webapps).
