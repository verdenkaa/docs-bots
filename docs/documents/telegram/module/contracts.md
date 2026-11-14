---
title: "DTO RAG API"
sidebar_position: 2.1
toc_max_heading_level: 2
---

[Общие типы](../../rag-core/contracts.md): DTO, PositiveId, NonNegativeInt, NonEmptyText, AwareDatetime, UUID, Annotated, Literal, Field, QuestionInput, RagResult, ImageRef и DeliveryAttempt.

~~~python
class AccessDenied(Exception):
    pass

class NotFound(Exception):
    pass

class Conflict(Exception):
    pass
~~~

## Приём

~~~python
from typing import Self
from pydantic import model_validator

class NewQuestion(DTO):
    update_id: NonNegativeInt
    user_id: PositiveId
    chat_id: PositiveId
    received_at: AwareDatetime
    input: QuestionInput

class IntakeResult(DTO):
    kind: Literal['accepted', 'duplicate', 'handled', 'busy', 'denied', 'ignored', 'invalid_update', 'retry_later']
    request_id: PositiveId | None = None
    delivery_id: PositiveId | None = None
    case_id: UUID | None = None

    @model_validator(mode='after')
    def check_intake(self) -> Self:
        """Принятый вопрос имеет ID; занятость не создаёт новый request."""
        if self.kind == 'accepted' and self.request_id is None:
            raise ValueError('accepted требует request_id')
        if self.kind == 'busy' and self.request_id is not None:
            raise ValueError('busy не содержит ID нового вопроса')
        return self

class SupportAction(DTO):
    kind: Literal['offer', 'start', 'confirm', 'decline']
    request_id: PositiveId | None = None
    case_id: UUID | None = None
    reason: Literal['dissatisfied', 'human_request'] | None = None

    @model_validator(mode='after')
    def check_action(self) -> Self:
        """Потребовать ключи, соответствующие стадии ручного сценария."""
        if self.kind == 'start' and self.request_id is None:
            raise ValueError('start требует request_id')
        if self.kind in ('confirm', 'decline') and self.case_id is None:
            raise ValueError('Подтверждение или отказ требуют case_id')
        if (self.kind == 'offer') != (self.reason is not None):
            raise ValueError('reason задаётся только для offer и обязателен в нём')
        return self

class SupportCommand(DTO):
    update_id: NonNegativeInt
    user_id: PositiveId
    chat_id: PositiveId
    received_at: AwareDatetime
    action: SupportAction
~~~

| Поле | Тип | Зачем / обязательное правило | Пример |
| --- | --- | --- | --- |
| update_id | NonNegativeInt | Дедупликация всех update одного бота, включая управление и пропуски. | 10001 |
| user_id, chat_id | PositiveId | Отправитель и личный чат доставки, тип чата проверен адаптером. | 123456789 |
| received_at | AwareDatetime | Первый приём, неизменен при повторе. | `"2026-11-14T12:00:00Z"` |
| input | QuestionInput | Текст либо Telegram file_id и подпись. | `{"kind":"text","text":"Где фильтр?"}` |
| IntakeResult.kind | перечисление | accepted требует request_id; duplicate возвращает прежние ссылки; handled — действие; busy/denied/ignored окончательны; retry_later не подтверждает приём. | `"busy"` |
| request_id, delivery_id, case_id результата | ID или None | Только реально созданные/найденные объекты. Для busy нет нового request_id. В HTTP-ответ Telegram ID не передаются. | `delivery_id=2086` |
| SupportAction.kind | перечисление | offer показывает кнопку; start начинает ожидание; confirm/decline относятся к case. | `"start"` |
| request_id действия | PositiveId или None | Для start обязателен; для текстового offer без ID выбираем последний AnsweredOutcome с подтверждённой текстовой частью сотруднику, даже если изображения ещё в пути. | 1042 |
| case_id действия | UUID или None | Обязателен для confirm/decline. Если переданы оба ID, они относятся к одному обращению. | `"bb32ecbf-082b-4497-a41f-4b61aa56a139"` |
| reason | перечисление или None | Обязателен для offer, иначе None; недовольство или просьба о человеке. | `"dissatisfied"` |
| SupportCommand.action | SupportAction | Управление, не новый вопрос в Core. Остальные поля — как у NewQuestion. | `{"kind":"start","request_id":1042}` |

Блоки кода задают структуру и валидаторы, синхронизированные с каркасом. Взаимная обязательность полей проверяется model_validator; проверки прав, принадлежности и стадии атомарный переход БД должен повторить при реализации. Для ручного вызова нужен вопрос с AnsweredOutcome и подтверждённой текстовой частью ответа (включая length_fallback). Изображения могут ещё отправляться. До такой квитанции ожидание не создаётся.

## Доступ и тикет

~~~python
from typing import Self
from pydantic import model_validator

class Principal(DTO):
    user_id: PositiveId
    role: Literal['employee', 'operator', 'admin']

class AccessRecord(DTO):
    user_id: PositiveId
    enabled: bool
    role: Literal['employee', 'operator', 'admin']

class SupportCase(DTO):
    case_id: UUID
    request_id: PositiveId
    user_id: PositiveId
    state: Literal['waiting', 'awaiting_confirmation', 'creating', 'open', 'closed', 'cancelled']
    reason: Literal['dissatisfied', 'human_request', 'out_of_scope', 'no_evidence']
    check_at: AwareDatetime | None = None
    ticket_url: NonEmptyText | None = None
    created_at: AwareDatetime | None = None
    closed_at: AwareDatetime | None = None

    @model_validator(mode='after')
    def check_ticket_state(self) -> Self:
        """Проверить обязательные поля открытого тикета и ручного ожидания."""
        if self.state in ('open', 'closed'):
            if self.ticket_url is None or self.created_at is None:
                raise ValueError('Созданный тикет требует URL и created_at')
        elif self.ticket_url is not None or self.created_at is not None:
            raise ValueError('До создания тикета нет URL или created_at')
        if (self.state == 'closed') != (self.closed_at is not None):
            raise ValueError('closed_at задаётся только для closed')
        if self.state in ('waiting', 'awaiting_confirmation') and self.check_at is None:
            raise ValueError('Ручное ожидание требует сохранённого check_at')
        if self.closed_at is not None and self.closed_at < self.created_at:
            raise ValueError('Закрытие не может предшествовать созданию')
        return self

class HistoryEntry(DTO):
    request_id: PositiveId
    question_text: NonEmptyText
    answer_text: str | None = None
    outcome: Literal['answered', 'support', 'notice']
    received_at: AwareDatetime

class TicketMessage(DTO):
    message_id: PositiveId
    case_id: UUID
    author_id: PositiveId
    author_role: Literal['employee', 'operator', 'admin']
    text: NonEmptyText
    created_at: AwareDatetime

class TicketView(DTO):
    case: SupportCase
    history: list[HistoryEntry]
    messages: list[TicketMessage]

class MetricsSnapshot(DTO):
    from_at: AwareDatetime
    to_at: AwareDatetime
    requests: NonNegativeInt
    answered: NonNegativeInt
    escalated: NonNegativeInt
    dissatisfied: NonNegativeInt
    reply_duration_ms: list[NonNegativeInt]
    ticket_duration_ms: list[NonNegativeInt]
    topics: dict[NonEmptyText, NonNegativeInt] | None = None

    @model_validator(mode='after')
    def check_period(self) -> Self:
        """Период метрик полуоткрытый и имеет положительную длительность."""
        if self.from_at >= self.to_at:
            raise ValueError('from_at должен предшествовать to_at')
        return self
~~~

| Поле | Тип | Смысл / правило | Пример |
| --- | --- | --- | --- |
| Principal.user_id, role | PositiveId, роль | Проверенная личность; не читаем роль из тела запроса. Минимальные роли интерфейса, не доступа к документам. | `123, "employee"` |
| AccessRecord.user_id, enabled, role | ID, bool, роль | Разрешённый сотрудник и полномочия; изменяет администратор. | `123, True, "operator"` |
| SupportCase.case_id | UUID | Один case на исходный вопрос; открытый case и есть тикет собственной системы. | UUID |
| request_id, user_id | PositiveId | Исходный вопрос и владелец; чужой пользователь не может управлять case. | `1042, 123` |
| state | перечисление | waiting/awaiting_confirmation ещё не тикет; open/closed доступны по ссылке. | `"waiting"` |
| reason | перечисление | Причина ручной либо автоматической передачи. | `"no_evidence"` |
| check_at | UTC-время или None | Для start: первый приём нажатия + 10 минут; повтор не сдвигает срок. | `"2026-11-14T12:10:00Z"` |
| ticket_url | str или None | Только после создания, для open/closed. | `"https://bot.example/tickets/<uuid>"` |
| created_at, closed_at | UTC-время или None | Фактическое создание/закрытие тикета, не начало ручного ожидания. | None / UTC |
| HistoryEntry.request_id, question_text | ID, текст | Снимок исходного вопроса; для изображения — prepared_text, подпись или отметка об изображении при ошибке OCR. | `1042, "Ошибка AUTH-17"` |
| answer_text, outcome, received_at | текст или None, исход, время | Реальный ответ, тип исхода и время вопроса. | `None, "support", "2026-11-14T12:00:00Z"` |
| TicketMessage.message_id, case_id | ID, UUID | Реплика в конкретном тикете. | `9, UUID(...)` |
| author_id, author_role, text, created_at | ID, роль, текст, время | Автор из Principal и содержание сообщения. | `123, "employee", "Не открывается", UTC` |
| TicketView.case, history, messages | DTO и списки | Тикет, контекст и дальнейший разговор. | `history=[...], messages=[]` |
| MetricsSnapshot.from_at, to_at | UTC-время | Полуоткрытый период статистики. | `[12:00,13:00)` |
| requests, answered, escalated, dissatisfied | NonNegativeInt | Наблюдаемые счётчики; не процент решённых вопросов. | `10,7,3,1` |
| topics | dict[str,int] или None | Распределение тем; None — метод разметки пока не выбран, не выдумываем категории. | None |
| reply_duration_ms | list[NonNegativeInt] | confirmed_at первой нормальной текстовой квитанции минус received_at вопроса; без дубликатов. | `[8300,12000]` |
| ticket_duration_ms | list[NonNegativeInt] | Для закрытых тикетов: closed_at минус received_at исходного вопроса. Закрытие не доказывает успех бота. | `[180000]` |

У open/closed обязательны ticket_url и created_at; closed требует closed_at. Для ожидания ticket_url отсутствует. Эти взаимные ограничения реализованы валидатором check_ticket_state в каркасе.

Проектный минимум истории — один исходный вопрос и его ответ, если он был. Весь диалог и промпты не включаются. Это не добавляет память диалога LLM; расширить историю можно позднее.

## Доставка

~~~python
from typing import Self
from pydantic import model_validator

class InlineAction(DTO):
    text: NonEmptyText
    callback_data: NonEmptyText | None = None
    url: NonEmptyText | None = None

    @model_validator(mode='after')
    def check_target(self) -> Self:
        """Кнопка имеет одно назначение; callback укладывается в лимит UTF-8."""
        if (self.callback_data is None) == (self.url is None):
            raise ValueError('Нужно ровно одно из callback_data и url')
        if self.callback_data is not None and len(self.callback_data.encode('utf-8')) > 64:
            raise ValueError('callback_data превышает 64 байта')
        return self

class TelegramEntity(DTO):
    type: Literal['bold', 'italic', 'code', 'pre', 'text_link']
    offset: NonNegativeInt
    length: PositiveId
    url: NonEmptyText | None = None

    @model_validator(mode='after')
    def check_link(self) -> Self:
        """URL задаётся только для entity типа text_link и обязателен для неё."""
        if (self.type == 'text_link') != (self.url is not None):
            raise ValueError('URL требуется только для text_link')
        return self

class TextPart(DTO):
    kind: Literal['text']
    text: NonEmptyText
    entities: list[TelegramEntity] = Field(default_factory=list)
    actions: list[InlineAction] = Field(default_factory=list)

    @model_validator(mode='after')
    def check_text_entities(self) -> Self:
        """Проверить бюджет текста и границы entities в кодовых единицах UTF-16."""
        boundaries = {0}
        position = 0
        for character in self.text:
            position += len(character.encode('utf-16-le')) // 2
            boundaries.add(position)
        if position > 4096:
            raise ValueError('Текст превышает бюджет 4096 единиц UTF-16')
        for entity in self.entities:
            if entity.offset not in boundaries or entity.offset + entity.length not in boundaries:
                raise ValueError('Entity выходит за текст или разрезает суррогатную пару')
        return self

class ImagePart(DTO):
    kind: Literal['image']
    image: ImageRef
DeliveryPart = Annotated[TextPart | ImagePart, Field(discriminator='kind')]

class ClaimedDelivery(DTO):
    attempt: DeliveryAttempt
    user_id: PositiveId
    chat_id: PositiveId
    request_id: PositiveId | None = None
    purpose: Literal['answer', 'notice', 'status', 'busy', 'denied', 'first_question', 'support_offer', 'support_confirmation', 'ticket_link']
    content: RagResult | None = None
    case: SupportCase | None = None
    parts: list[DeliveryPart] | None = None
    next_part: NonNegativeInt = 0

    @model_validator(mode='after')
    def check_delivery(self) -> Self:
        """Проверить наличие данных для сборки частей и сохранённый прогресс."""
        if self.purpose in ('answer', 'notice') and self.content is None:
            raise ValueError('answer/notice требуют content')
        if self.purpose == 'answer' and (not self.content.sources):
            raise ValueError('Доставка ответа требует источников')
        if self.purpose in ('status', 'support_offer') and self.request_id is None:
            raise ValueError('status/support_offer требуют request_id')
        if self.purpose in ('support_confirmation', 'ticket_link') and self.case is None:
            raise ValueError('Доставка поддержки требует case')
        if self.purpose == 'ticket_link' and self.case.ticket_url is None:
            raise ValueError('ticket_link требует созданный тикет')
        if self.case is not None:
            if self.case.user_id != self.user_id:
                raise ValueError('Тикет принадлежит другому сотруднику')
            if self.request_id is not None and self.case.request_id != self.request_id:
                raise ValueError('Тикет относится к другому вопросу')
        if self.parts is None:
            if self.next_part != 0:
                raise ValueError('Без плана next_part равен нулю')
        elif self.next_part > len(self.parts):
            raise ValueError('next_part выходит за сохранённый план')
        return self

class Sent(DTO):
    kind: Literal['sent']
    message_id: PositiveId
    variant: Literal['normal', 'length_fallback'] = 'normal'

class SendFailed(DTO):
    kind: Literal['failed']
    retryable: bool
    uncertain: bool
    retry_after_seconds: NonNegativeInt | None = None
SendResult = Annotated[Sent | SendFailed, Field(discriminator='kind')]
~~~

| Поле | Тип | Смысл / правило | Пример |
| --- | --- | --- | --- |
| InlineAction.text | str | Подпись; ровно одно из callback_data и url задано. | `"Позвать оператора"` |
| callback_data | str или None | Короткое действие, владелец/состояние проверяются в БД. | `"s:1042"` |
| url | str или None | Переход в созданный тикет, не callback. | `"https://bot.example/tickets/…"` |
| TelegramEntity.type, offset, length, url | тип, числа, URL или None | Индексы UTF-16 видимого текста; URL только для text_link. parse_mode не используем. | `"bold",0,5,None` |
| TextPart.text, entities, actions | текст и списки | Одна уже отформатированная текстовая отправка. | `"Откройте фильтр",[],[]` |
| ImagePart.image | ImageRef | Фото отдельно, без длинной подписи; описание уже в тексте. | `ImageRef(...)` |
| ClaimedDelivery.attempt | DeliveryAttempt | Право конкретной доставки на запись прогресса. | `delivery_id=2085` |
| user_id, chat_id, request_id | ID, ID, ID или None | Получатель и исходный вопрос, если он есть. | `123,123,1042` |
| purpose | перечисление | answer/notice требуют content; busy/denied/first_question строят стандартное пояснение; status/offer — request_id; confirmation/link — case. Для ticket_link case.ticket_url задан. | `"support_offer"` |
| content, case | DTO или None | Полные данные для чистой сборки частей без недостающих запросов к БД. | `RagResult(...), None` |
| parts, next_part | список или None, индекс | Неизменяемый план и первая неподтверждённая часть; план сохраняем до Telegram. | `None,0` |
| Sent.message_id, variant | ID, вариант | Квитанция Telegram и факт резервного текста. | `77,"normal"` |
| SendFailed.retryable, uncertain, retry_after_seconds | bool, bool, число или None | Повтор, неизвестный исход и задержка конкретной отправки. | `True,True,3` |

Callback-формат: `a:<request_id>` — недовольство/offer; `s:<request_id>` — start; `y:<case_uuid>` и `n:<case_uuid>` — confirm/decline. Все варианты укладываются в 64 байта; состояние и принадлежность проверяются даже для вручную сформированного callback. Текстовые «оператор»/«человек» вызывают offer только после доставленного ответа.

У answer возможна реакция «Ответ не помог»; это не кнопка оператора. Кнопка вызова появляется в support_offer, а ticket_link содержит переход к тикету. Поля union и взаимные ограничения проверяются валидаторами, а не только аннотациями.

Ограничения Telegram и формат entities: [Bot API](https://core.telegram.org/bots/api#messageentity), [кнопки](https://core.telegram.org/bots/api#inlinekeyboardbutton).
