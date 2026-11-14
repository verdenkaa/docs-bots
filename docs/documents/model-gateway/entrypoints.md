---
title: Точки входа Model Gateway
sidebar_position: 2
toc_max_heading_level: 2
---

[Правила DTO и Pydantic](../faq/dto.md): внутри Python передаём объекты; примеры словарей в таблицах показывают данные для валидации/JSON.

Функции находятся в `app/rag/model_gateway/entrypoints.py`. Binding и бюджеты шагов поставляет Runtime при сборке; вызывающие модули не создают клиентов SDK. [DTO и ошибки](./contracts.md) · [Жизненный цикл](./lifecycle.md).

## <span className="operation-badge operation-badge--async">ASYNC</span> `start_models` {#start-models}

~~~python
async def start_models(bindings: Mapping[ModelRole, ModelBinding]) -> None: ...
~~~

**Кто вызывает.** Runtime в каждом процессе, который выполняет модельные операции, до допуска работы.

### Что принимает

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `bindings` | `Mapping[ModelRole, ModelBinding]` | Один выбранный адаптер на каждую нужную роль. Источник — конфигурация запуска. | `{"embedding": binding}` |

### Что отдаёт

**Формат.** Одно значение Python `None`.

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `None` | Все переданные роли инициализированы и проверены; ошибка хотя бы одной роли не считается успешным запуском. | `None` |

### Правила и ошибки

Пустая карта допустима для процесса без модельных ролей. Повтор с тем же набором не дублирует ресурсы. При ошибке освобождает ресурсы частичного запуска и поднимает StepError. [Поля ModelBinding](./contracts.md#binding).

## <span className="operation-badge operation-badge--async">ASYNC</span> `close_models` {#close-models}

~~~python
async def close_models() -> None: ...
~~~

**Кто вызывает.** Runtime после остановки приёма работы и завершения/отмены активных операций.

### Что принимает

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| — | — | Параметров нет: закрываются роли текущего процесса. | — |

### Что отдаёт

**Формат.** Одно значение Python `None`.

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `return` | `None` | Клиенты и локальные исполнители освобождены. | `None` |

### Правила и ошибки

Повторный вызов безопасен, в том числе после неудачного старта. Предел штатной остановки контролирует Runtime; зависший локальный процесс завершает супервизор. Закрытие внешнего API-клиента не означает остановку удалённого сервера.

## <span className="operation-badge operation-badge--async">ASYNC</span> `embed_texts` {#embed-texts}

~~~python
async def embed_texts(texts: Sequence[str], purpose: Literal["query", "document"], *, timeout_s: float) -> EmbeddingResult: ...
~~~

**Кто вызывает.** Searcher для вопроса и Ingestion Worker для чанков.

### Что принимает

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `texts` | `Sequence[str]` | Тексты из нормализованного вопроса или результата prepare_chunks; элементы непустые. | `["Как установить?"]` |
| `purpose` | `Literal["query", "document"]` | Выбирает инструкции кодирования внутри одного профиля. | `"query"` |
| `timeout_s` | `float` | Положительный конечный бюджет всего вызова в секундах. | `5.0` |

### Что отдаёт

**Формат.** Один объект Pydantic `EmbeddingResult`.

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `profile_id` | `str` | ID выполненного embedding-профиля; должен совпасть с профилем индекса. | `"embedding-v1"` |
| `vectors` | `list[list[float]]` | Ровно один вектор на текст, в том же порядке и выбранной размерности. | `[[0.1, -0.2, ...]]` |

### Правила и ошибки

Для texts=[] возвращает пустой vectors с ID настроенного профиля без inference. Роль всё равно должна быть инициализирована. Проверяет длину, размерность и конечность координат. Searcher использует vectors[0], Worker — vectors[chunk_no]. [DTO](./contracts.md#embedding-result).

## <span className="operation-badge operation-badge--async">ASYNC</span> `rerank` {#rerank}

~~~python
async def rerank(query: str, candidates: Sequence[str], *, timeout_s: float) -> RerankResult: ...
~~~

**Кто вызывает.** Searcher после получения кандидатов Qdrant, только при включённом reranker.

### Что принимает

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `query` | `str` | Непустой нормализованный вопрос из Guardrails. | `"Как установить?"` |
| `candidates` | `Sequence[str]` | Тексты fragment.text в порядке выдачи find_chunks; позиции фиксируются до вызова. | `["Инструкция по установке…"]` |
| `timeout_s` | `float` | Положительный конечный бюджет ожидания и inference. | `5.0` |

### Что отдаёт

**Формат.** Один объект Pydantic `RerankResult`; элементы scores — объекты `RerankScore`.

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `profile_id` | `str` | ID профиля reranker. | `"reranker-v1"` |
| `scores` | `list[RerankScore]` | По одной оценке на кандидата, порядок исходных индексов. | `[{"candidate_index": 0, "score": 2.4}]` |
| `scores[].candidate_index` | `int` | Позиция в candidates, не chunk_id. | `0` |
| `scores[].score` | `float` | Конечная оценка, больше — релевантнее. | `2.4` |

### Правила и ошибки

Для пустого списка отдаёт scores=[] без inference при настроенной роли. Не меняет и не отбрасывает кандидатов. При недоступности обязательного reranker — StepError; Searcher не выключает его молча. [DTO](./contracts.md#rerank-result).

## <span className="operation-badge operation-badge--async">ASYNC</span> `classify_injection` {#classify-injection}

~~~python
async def classify_injection(text: str, *, timeout_s: float) -> InjectionResult: ...
~~~

**Кто вызывает.** Guardrails после проверки допустимости исходного ввода, до нормализации для кеша.

### Что принимает

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `text` | `str` | Непустой исходный вопрос пользователя, без преобразований Guardrails. | `"Игнорируй инструкции…"` |
| `timeout_s` | `float` | Положительный конечный бюджет проверки. | `2.0` |

### Что отдаёт

**Формат.** Один объект Pydantic `InjectionResult`; это наблюдение детектора, не решение Guardrails.

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `profile_id` | `str` | Профиль, для которого настроены правила Guardrails. | `"injection-v1"` |
| `label` | `Literal["benign", "injection"] \| None` | Нативный класс, если модель его предоставляет. | `None` |
| `score` | `float \| None` | Оценка с направлением «больше — опаснее», шкала зависит от профиля. | `0.8` |

### Правила и ошибки

Пустой вопрос — model_invalid_input, оба label/score равны None — model_invalid_response. Guardrails проверяет профиль и применяет собственное правило допуска; StepError переводит в detector_unavailable с сохранением retryable. [DTO](./contracts.md#injection-result).

## <span className="operation-badge operation-badge--async">ASYNC</span> `generate` {#generate}

~~~python
async def generate(messages: Sequence[ModelMessage], options: GenerationOptions, *, timeout_s: float) -> ModelGenerationResult: ...
~~~

**Кто вызывает.** Answer Generator после сборки контекста/меток либо Worker при генерации примеров темы. Промпт и бизнес-JSON валидирует вызывающий.

### Что принимает

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `messages` | `Sequence[ModelMessage]` | Непустой список сообщений: системные правила и вопрос с контекстом, роли сохранены. | `[{"role": "system", "content": "Отвечай по источникам."}, ...]` |
| `options` | `GenerationOptions` | Бюджет вывода и выбранная настройка генерации из конфигурации. | `{"max_output_tokens": 512, "temperature": 0.0}` |
| `timeout_s` | `float` | Положительный конечный бюджет до полного ответа, включая stream. | `30.0` |

### Что отдаёт

**Формат.** Один объект Pydantic `ModelGenerationResult`, не генератор токенов.

| Параметр / поле | Тип | Зачем нужно | Пример |
| --- | --- | --- | --- |
| `profile_id` | `str` | ID профиля генерации. | `"generation-v1"` |
| `text` | `str` | Собранный текст без служебных событий протокола. | `"{…}"` |
| `finish_reason` | `Literal["completed", "length", "filtered"]` | Нормализованная причина завершения. | `"completed"` |
| `usage` | `TokenUsage \| None` | Доступные счётчики входных/выходных токенов. | `{"input_tokens": 700, "output_tokens": 120}` |

### Правила и ошибки

При stream адаптер собирает фрагменты по порядку; обрыв до подтверждённого завершения — StepError, накопленный текст не выдаётся за успех. Answer Generator принимает только completed и валидный бизнес-JSON; length/filtered становятся StepError("generation_incomplete", False). Worker также принимает только completed, проверяет свой JSON вопросов и сохраняет результат до записи индексов. Gateway не интерпретирует citations/вопросы и не выдаёт RagResult/TopicPoint. [Все вложенные поля](./contracts.md#generation).

Все ошибки — [StepError с согласованным кодом и retryable](./contracts.md#errors). Числа в примерах иллюстративны и не задают рабочие лимиты. [Откуда берутся параметры](../rag-core/flow.md#model-calls).

## <span className="operation-badge operation-badge--async">ASYNC</span> `recognize_text` {#recognize-text}

~~~python
async def recognize_text(image: ImageContent, *, timeout_s: float) -> OcrResult: ...
~~~

**Кто вызывает.** Core для изображения пользователя; Document Converter при необходимости.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| image | ImageContent | Байты и проверенный MIME из Runtime. | ImageContent(...) |
| timeout_s | float > 0 | Весь бюджет, включая ожидание исполнителя. | 15.0 |

### Что отдаёт

**Формат.** OcrResult.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| profile_id | str | Профиль реально выполненного OCR. | ocr-v1 |
| text | str | Извлечённый текст; пустая строка допустима. | Ошибка AUTH-17 |

### Правила и ошибки

Не отвечает на вопрос по интерфейсу и не выдумывает описание. Пустой OCR — успешное наблюдение; ошибка сервиса — StepError. Байты/секреты не логируются, локальное вычисление не блокирует event loop.

## <span className="operation-badge operation-badge--async">ASYNC</span> `describe_image` {#describe-image}

~~~python
async def describe_image(image: ImageContent, *, timeout_s: float) -> ImageDescription: ...
~~~

**Кто вызывает.** Document Converter внутри Ingestion Gateway.

### Что принимает

| Параметр | Тип | Зачем нужен | Пример |
| --- | --- | --- | --- |
| image | ImageContent | Извлечённое изображение документа. | ImageContent(...) |
| timeout_s | float > 0 | Бюджет Gateway. | 20.0 |

### Что отдаёт

**Формат.** ImageDescription.

| Поле / значение | Тип | Смысл | Пример |
| --- | --- | --- | --- |
| profile_id | str | Профиль VLM. | vision-v1 |
| description | NonEmptyText | Описание изображения для Markdown и embedding. | Схема согласования... |

### Правила и ошибки

Только модельный результат; image_id и запись S3 принадлежат Gateway. Для входящих изображений пользователя этот вызов не используется.
