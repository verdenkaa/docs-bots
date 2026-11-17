---
title: "Точки входа RAG API"
sidebar_position: 2
toc_max_heading_level: 2
---



[DTO API](./contracts.md) · [Общие DTO](../../rag-core/contracts.md) · [Путь вопроса](../../rag-core/flow.md). Все результаты ниже — объекты DTO внутри Python; JSON возникает только на внешней границе.

## <span className="operation-badge operation-badge--async">ASYNC</span> `handle_telegram_update` {#handle-telegram-update}

~~~python
async def handle_telegram_update(update: Mapping[str, object]) -> IntakeResult: ...
~~~

**Кто вызывает.** Внутренний HTTP-адаптер после проверки секрета; updates поступают только от poller.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| update | Mapping[str, object] | Декодированный Telegram Update. | &#123;"update_id":10001,"message":&#123;...&#125;&#125; |
| message.from.username / callback_query.from.username | str или отсутствует | Текущее имя для допуска, нормализуется в lowercase без @. | employee_name |
| callback_query.message.message_id | int | Сообщение с кнопкой для сверки с квитанцией. | 77 |
| message.from.id | int | Идентичность сотрудника. | 123456789 |
| callback_query.from.id | int | Идентичность сотрудника. | 123456789 |
| message.chat | объект | Только type=private; ID назначения. | &#123;"id":123456789,"type":"private"&#125; |
| callback_query.message.chat | объект | Только type=private; ID назначения. | &#123;"id":123456789,"type":"private"&#125; |
| message.text | текст | Вопрос; photo даёт file_id выбранного размера. Image-document допустим по разрешённому MIME. | "Где фильтр?" |
| photo | список PhotoSize | Вопрос; photo даёт file_id выбранного размера. Image-document допустим по разрешённому MIME. | "Где фильтр?" |
| caption | текст | Вопрос; photo даёт file_id выбранного размера. Image-document допустим по разрешённому MIME. | "Где фильтр?" |
| callback_query.id | строки | Подтверждение нажатия и компактное действие. | "cq-1" |
| data | строки | Подтверждение нажатия и компактное действие. | "s:1042" |

### Что отдаёт

**Формат.** Один IntakeResult; поля и инварианты — в [DTO](./contracts.md#приём).

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| kind | accepted / duplicate / handled / busy / denied / ignored | Надёжно завершённый приём; только accepted создаёт вопрос. | "accepted" |
| request_id / delivery_id / case_id | ID или None | Реальные ссылки на созданные/найденные объекты. | 1042 / None / None |
| kind | invalid_update / retry_later | Некорректный вход либо техническая необходимость повтора. | "retry_later" |

### Правила и ошибки

Проверяет структуру личного чата и разбирает сообщение либо старое действие Support. Время приёма берёт в RAG API. `/start`, `/help` (аргументы и `@BotName` поддерживаются), обычный текст и изображения вызывают [register_bot_notice](../../infrastructure/postgresql/rag-api.md#register-bot-notice): приглашение открыть Mini App доставляется через notice/Outbox без допуска сотрудника, RAG-вопроса и активного слота. Остальные slash-команды, группы и неподдерживаемые updates сохраняются как ignored без ответа.

Telegram-вход не вызывает register_question, поиск или Shortcut Interceptor. Классификатор остаётся самостоятельным модулем; интеграция в Mini App находится вне этого изменения. HTTP-контракты `/api/v1/*` не меняются.

Ledger проверяется перед созданием notice; повтор сохраняет прежний исход, включая исторический accepted/duplicate с request_id. StorageError даёт retry_later. Старые callback продолжают handle_support_action с проверкой допуска и сохранённой кнопки. Единственный владелец answerCallbackQuery — intake; сбой квитанции не откатывает уже применённое действие.

## <span className="operation-badge operation-badge--sub">SUB</span> `handle_rag_result` {#handle-rag-result}

~~~python
async def handle_rag_result(event: DeliveryRef) -> ConsumeDecision: ...
~~~

**Кто вызывает.** Адаптер rag.results.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| event.delivery_id | PositiveId | ID самостоятельной доставки из RabbitMQ. | 2085 |

### Что отдаёт

**Формат.** Одна строка Python.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | ack | Сохранён переход или уведомление уже не требует работы. | "ack" |
| return | retry_broker | Состояние/коммит PostgreSQL неизвестны. | "retry_broker" |

### Правила и ошибки

claim_delivery(delivery_id) отдаёт ClaimedDelivery либо None. Если parts отсутствуют, строит build_delivery_parts и сохраняет store_delivery_parts до первого внешнего вызова. Берёт части с next_part, вызывает send_answer и после подтверждения record_delivery_part. При временной ошибке — schedule_delivery_retry; после последней части либо окончательной ошибки — complete_delivery. Статус/предложение проверяются на актуальность при claim. Потерявшая право попытка не продолжает отправку следующих частей. Ни поиск, ни создание тикета здесь не повторяются. Последующий отзыв допуска не отменяет доставку ранее принятого request; отправляем оставшиеся части с обычными повторами.

## <span className="operation-badge operation-badge--call">CALL</span> `build_delivery_parts` {#build-delivery-parts}

~~~python
def build_delivery_parts(delivery: ClaimedDelivery, source_base_url: str) -> list[DeliveryPart]: ...
~~~

**Кто вызывает.** handle_rag_result, только если план ещё не сохранён.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| delivery | ClaimedDelivery | Контент/состояние и назначение из PostgreSQL. | purpose="answer", content=RagResult(...) |
| source_base_url | str | Проверенный адрес RAG API из конфигурации для резервных ссылок источников без source_url. | "https://bot.example" |

### Что отдаёт

**Формат.** Непустой упорядоченный список TextPart/ImagePart.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return[0] | TextPart | Текст, ссылки источников, форматирование и допустимые действия. | &#123;"kind":"text","text":"Откройте…","entities":[],"actions":[]&#125; |
| return[1:] | list[ImagePart] | Нужные файлы по уникальным ID, если они есть. | [&#123;"kind":"image","image":&#123;...&#125;&#125;] |

### Правила и ошибки

Чистая сборка без I/O. Для answer/notice использует content; для status/busy/denied/first_question — стандартный текст; offer даёт кнопку s:request_id; confirmation — y/n:case_id; ticket_link использует case.ticket_url. Обычный ответ может предложить «Ответ не помог» (a:request_id), но не кнопку оператора. Переводит Markdown в видимый текст + entities с UTF-16-смещениями; image-маркеры становятся описаниями, файлы — отдельными частями. Источник группируется по document_id; ссылка использует проверенный HTTP(S) source_url из SourceRef, а при None — /sources/&#123;document_id&#125; на source_base_url. Это правило одинаково для обычного списка и сокращённого источника при длинном ответе. Видимый текст ограничен 4096 единицами UTF-16, из них до 512 — источники. При переполнении сохраняет хотя бы один источник либо даёт короткий резервный текст и реакцию недовольства. План один раз сохраняется в PostgreSQL и при повторе не пересобирается.

## <span className="operation-badge operation-badge--async">ASYNC</span> `send_answer` {#send-answer}

~~~python
async def send_answer(chat_id: int, part: DeliveryPart) -> SendResult: ...
~~~

**Кто вызывает.** handle_rag_result после сохранения плана.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| chat_id | int | Личный чат из ClaimedDelivery. | 123456789 |
| part | DeliveryPart | Одна неизменяемая часть, не весь RagResult. | TextPart(...) |

### Что отдаёт

**Формат.** Один вариант SendResult.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| kind / message_id / variant | sent / int / normal или length_fallback | Квитанция одной отправки Telegram. | "sent" / 77 / "normal" |
| kind / retryable / uncertain / retry_after_seconds | failed / bool / bool / int или None | Ошибка конкретной части. | "failed" / True / True / None |

### Правила и ошибки

Для TextPart вызывает sendMessage с entities, без parse_mode. Для ImagePart получает [get_image(image_id)](../../infrastructure/s3.md#get-image), вызывает [prepare_telegram_photo](../../infrastructure/media.md#prepare-telegram-photo) и отправляет подготовленную JPEG-копию через sendPhoto без длинной подписи. Сбой S3 не отбрасывает уже доставленный текст. Учитывает общий/чатовый лимитер и retry_after, сам не запускает цикл повторов. Таймаут после возможной отправки даёт uncertain=True. При явном отклонении текста из-за длины допустима одна отправка «Ответ не помещается. Пожалуйста, задайте более узкий вопрос.»; успех отмечен length_fallback. Неизвестный исход исходной отправки не запускает этот fallback. [Bot API](https://core.telegram.org/bots/api#sendmessage).

## <span className="operation-badge operation-badge--task">TASK</span> `run_api_maintenance` {#run-api-maintenance}

~~~python
async def run_api_maintenance(limit: int) -> int: ...
~~~

**Кто вызывает.** Runtime в фоновом цикле RAG API.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| limit | int > 0 | Максимальная пачка для каждого фонового прохода. | 20 |

### Что отдаёт

**Формат.** Целое число.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| return | int >= 0 | Количество сохранённых фоновых переходов: статусы и действия поддержки. | 3 |

### Правила и ошибки

Берёт UTC now, вызывает queue_waiting_statuses(now,limit) и run_support_batch(limit). Статус создаётся один раз после 10 секунд без итога; 20 секунд — только метрика. Пустой проход ждёт, сбой повторяется с задержкой; deadline технических попыток задаётся отдельно.

## DTO {#dto}

Определения вынесены в [DTO RAG API](./contracts.md); общие RequestRef, DeliveryRef, RagResult и попытки — в [общую страницу](../../rag-core/contracts.md). Старый send_answer(chat_id, result) заменён отправкой одной части. Core публикует delivery_id, а не request_id в rag.results.

[Access и Support: функции](./service-entrypoints.md) · [Инфраструктурные функции](../../infrastructure/postgresql/rag-api.md)


### Фактически доставленное представление

`TextPart.variant` сохраняет заранее выбранный `normal`/`length_fallback` в плане.
Сначала formatter убирает оформление источников, если это позволяет сохранить полный
ответ. Если текст всё равно превышает 4096 единиц UTF-16, заменяет его короткой просьбой
сузить вопрос, без обрезки исходного ответа и без связанных изображений.
`send_answer` возвращает `Sent.delivered_text` с точным текстом успешной отправки;
для изображения это None. При явном отказе Telegram по длине допускается одна
резервная отправка; при таймауте исходной отправки она не выполняется.
Квитанция сохраняет текст и вариант в PostgreSQL. История Support использует эти
подтверждённые тексты, а метрика времени обычного ответа исключает length_fallback.
