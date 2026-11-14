---
title: DTO и Pydantic
sidebar_position: 1.2
---

Документация задаёт **целевые контракты MVP**, включая новые OCR/Support/Ingestion DTO. DTO, точки входа и контрактные тесты каркаса синхронизированы с текущими спецификациями. Взаимная обязательность полей проверяется model_validator; прикладные операции и адаптеры пока поднимают NotImplementedError. Наличие схемы и снятие warning не означают реализованный сценарий.

Канонические типы: [общие DTO](../rag-core/contracts.md), [RAG API](../telegram/module/contracts.md), [Ingestion](../ingestion/contracts.md), [Model Gateway](../model-gateway/contracts.md).

## Общий порядок

Передаваемые данные описываем моделями Pydantic v2, наследующими `rag.dto.DTO`. Между Python-модулями передаём объекты, поля читаем как `result.answer_text`. На границе RabbitMQ, Redis, JSON PostgreSQL и модельного API валидируем входные данные. Сигнатура функции сама по себе **не запускает** Pydantic: адаптер должен явно вызвать валидацию.

~~~python
from pydantic import TypeAdapter
from rag.contracts import RequestRef, GuardrailsResult

event = RequestRef.model_validate_json(b'{"request_id": 1042}')
request_id = event.request_id
wire_json = event.model_dump_json()

guardrails_adapter = TypeAdapter(GuardrailsResult)  # создаём один раз
decision = guardrails_adapter.validate_json('{"kind": "blocked"}')
~~~

| Задача | Как делать |
| --- | --- |
| Создать DTO в коде | `RequestRef(request_id=1042)`; вложенные DTO также передаём объектами. |
| Проверить Python-данные из адаптера | `RequestRef.model_validate(row)`; UUID и datetime уже должны иметь свои Python-типы. |
| Прочитать JSON | `Model.model_validate_json(raw)`; для union — заранее созданный `TypeAdapter(...).validate_json(raw)`. В JSON UUID и время представлены строками, это допустимо и в strict mode. |
| Сохранить JSON | `model_dump_json()` либо `model_dump(mode="json")` для драйвера, принимающего JSON-совместимый словарь. |
| Собрать другой DTO из результата | Явно выбрать его поля. Например, `RagResult(answer_text=cached.answer_text, sources=cached.sources, images=cached.images)`; поле поколения кеша не переносим. |
| Получить технические значения | Список векторов, число, `None`, `ConsumeDecision` остаются обычными Python-значениями. |

Примеры словарей в таблицах контрактов показывают данные для валидации/JSON. Возвращаемое значение внутри Python — указанный объект DTO, список объектов либо другой явно заданный тип.

## База и ограничения

Общая база и именованные ограничения определены в `app/rag/dto.py`. Этот блок также сверяется с кодом; ограничения применяются ко всем наследующим DTO.

~~~python
from typing import Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints


class DTO(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", allow_inf_nan=False)


PositiveId = Annotated[int, Field(gt=0)]
NonNegativeInt = Annotated[int, Field(ge=0)]
NonEmptyText = Annotated[str, StringConstraints(min_length=1, pattern=r"\S")]
~~~

## Что проверяем

- `strict=True`: не превращаем строку `"1042"` или `True` в числовой ID. JSON UUID/дат читаем через JSON-метод, а не через `json.loads` с последующей строгой проверкой Python-строк.
- `extra="forbid"`: неизвестные поля отклоняются. `None` и отсутствие поля различаются: nullable-поля без default обязательны. Поля с default, например SourceLocator.page/section и списки images, допускают отсутствие; точные defaults указаны в DTO владельца. Для восстановления разреженного локатора используем `locator.model_dump(exclude_unset=True)`.
- Числа конечны. ID вопроса и номер взятой попытки положительны; `chunk_no` начинается с 0, поколение пустого корпуса равно 0. В MVP только личные чаты: user_id и chat_id положительные; группы/каналы не регистрируются как вопросы.
- `kind` выбирает вариант результата; `route` выбирает вид outbox. Например, маршрут `ingestion.jobs` принимает только `JobRef`, а не `RequestRef`.
- Исходный `raw_question` допускает пустую строку: решение `invalid_input` принимает Guardrails. Нормализованный допущенный вопрос и готовый ответ должны содержать непустой текст.
- Model Gateway проверяет наличие label/score, непустой completed-ответ и технический формат чисел. Соответствие числа векторов числу входов, профиля индексу и citations найденным чанкам проверяет реализация соответствующего шага: один DTO не имеет всех этих данных.

Pydantic не проверяет принадлежность чанка редакции, действующий deadline в PostgreSQL, наличие реального файла и истинность ответа модели. Эти проверки остаются у владельцев операций. Данные результата после создания не следует менять по месту: для нового состояния создаём новый DTO с обычной валидацией; `model_construct` и обход проверки через `model_copy(update=...)` на границах не используем.

## Потоки и ошибки

`UploadInput` — обычный dataclass с `AsyncIterable[bytes]`, проверенным `UploadSource` и `document_id`. Это технический вход Python, не транспортный JSON. Gateway проверяет поток и входные значения; сериализовать поток в очередь нельзя. `CacheRead.compare_token` — внутренние bytes, также не самостоятельное сообщение очереди.

`ValidationError` обрабатывает адаптер границы: некорректный вход загрузки переводится в `invalid_upload`; непригодный ответ модели — в `StepError("model_invalid_response", False)`; ошибка уже сохранённых данных — в ошибку хранилища/диагностическое событие. Для RabbitMQ сохраняется правило отклонения некорректных событий из его контракта. Не записываем полный `ValidationError` в лог без фильтрации: он может содержать исходные тексты и секретные значения.

## Проверки

Из папки `app` с [Python 3.12.x](./python-environment.md):

~~~text
python -m pip install -c requirements.lock -e .
python -m unittest discover -s tests -v
~~~

`tests/test_contracts.py` проверяет валидацию, сериализацию и совместимость аргументов с реальными сигнатурами пути вопроса, коротких веток и индексации. Вспомогательная `validate_call_arguments()` не исполняет обработчики; возвраты зависимостей заданы тестовыми примерами. Это проверка совместимости контрактов, а не работающий RAG. [Порядок вызовов](../rag-core/flow.md#model-calls).

`tests/test_documented_contracts.py` сверяет полные наборы сигнатур, DTO, технических dataclass и именованных типов с Python-блоками действующей документации. Документ-владелец указан в `Contracts: docs/documents/...md.` в docstring модуля; общая база `rag.dto` сверяется с этой страницей. Проверяются defaults, типы, ограничения, конфигурация и валидаторы; пояснения и `Field(description=...)` могут отличаться. Исторические страницы `drafts/` исключены. Примеры разбираются как AST без исполнения; намеренные расхождения проверяются только в памяти.

Основа: [строгая валидация](https://docs.pydantic.dev/latest/concepts/strict_mode/), [дискриминируемые варианты](https://docs.pydantic.dev/latest/concepts/unions/#discriminated-unions).
