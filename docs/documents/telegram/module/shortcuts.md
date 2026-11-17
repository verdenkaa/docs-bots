---
title: Shortcut Interceptor
sidebar_position: 2.2
---

Локальный классификатор `rag.rag_api.shortcuts` обрабатывает типовые текстовые сообщения без Core, Qdrant и моделей. Реестр фраз и ответов находится в `shortcuts/registry.py`; для изменения набора формулировок редактируется только он.

## <span className="operation-badge operation-badge--call">CALL</span> `intercept_message` {#intercept-message}

~~~python
def intercept_message(text: str | None) -> ShortcutResult | None: ...
~~~

Принимает текст либо `None`. Возвращает `ShortcutResult(action="reply", response=...)`, `ShortcutResult(action="operator", response=None)` либо `None` (`miss`). Поиск — строгое совпадение всего нормализованного сообщения в одной хеш-таблице. Фраза «Привет, как настроить систему?» не совпадает с «Привет»; «Не вызывай оператора» не вызывает поддержку.

Классификатор не отправляет сообщения, не создаёт обращения, не читает БД и не управляет очередями. Результат возвращается отдельным объектом, чтобы вызывающая сторона не меняла реестр.

## <span className="operation-badge operation-badge--call">CALL</span> `normalize` {#normalize}

~~~python
def normalize(text: str) -> str: ...
~~~

Нормализация соответствует приложенному реестру: Unicode NFKC, `casefold`, замена `ё` на `е`, замена пунктуации, символов и подчёркиваний пробелами, свёртка пробелов. Например, `ПРИВЕТ!!!` → `привет`, `Позови, оператора!` → `позови оператора`. Подстроки, нечёткое сравнение и модельная классификация не используются.

## Результат классификации

Тип наследует общий `rag.dto.DTO` (Pydantic v2):

~~~python
class ShortcutResult(DTO):
    intent: NonEmptyText
    action: Literal["reply", "operator"]
    response: NonEmptyText | None = None

    @model_validator(mode="after")
    def check_response(self) -> "ShortcutResult":
        if (self.action == "reply") != (self.response is not None):
            raise ValueError("Only reply requires response")
        return self
~~~

`intent` — ключ группы фраз в реестре; `action` определяет действие, `response` обязателен только для `reply`. Промах обозначается `None`, отдельного DTO `miss` нет.

При импорте строится только локальный словарь. Неизвестные намерения, конфликт действий, пустая нормализованная фраза и совпадение фраз разных намерений вызывают `ValueError`; одинаковые варианты одного намерения объединяются. Пустой ответ `reply` отклоняется валидацией DTO.

## Место в текущем срезе

Классификатор и приложенный реестр остаются самостоятельным чистым модулем с прежним результатом `reply`/`operator`/`None`. Текущий Telegram-вход его не вызывает: бот только приглашает открыть Mini App. Встраивание классификатора в отдельный Mini App выполняется его владельцем; контракты `/api/v1/*` здесь не меняются.

## Telegram-навигация

`/start`, `/help` (аргументы и `@BotName` поддерживаются), обычный текст и изображения получают приглашение открыть Mini App через меню бота. Ответ сохраняется атомарно с ledger через register_bot_notice и доставляется обычным notice/Outbox. Допуск сотрудника не требуется; навигация не создаёт RAG-вопрос или активный слот. Прочие slash-команды, включая `/support` и `/operator`, сохраняются как ignored без ответа. Callback старых кнопок продолжают текущий Support.

Внешнего webhook и JSON Bot API в HTTP-ответе нет. Poller передаёт Update на внутреннюю `/internal/telegram/updates`, подтверждая его только после HTTP 200. При повторе используется прежний ledger, включая исторические ссылки на RAG-вопрос.
