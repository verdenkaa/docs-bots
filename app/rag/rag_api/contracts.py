"""Contracts: docs/documents/telegram/module/contracts.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import AwareDatetime, Field, model_validator

from rag.contracts import DeliveryAttempt, ImageRef, QuestionInput, RagResult
from rag.dto import DTO, NonEmptyText, NonNegativeInt, PositiveId


class AccessDenied(Exception):
    """Текущих прав недостаточно для действия."""
    pass


class NotFound(Exception):
    """Объект отсутствует или недоступен текущему пользователю."""
    pass


class Conflict(Exception):
    """Действие конфликтует с сохранённым состоянием или ключом повтора."""
    pass


class NewQuestion(DTO):
    """Новый вход Telegram для атомарной регистрации и проверки активного слота."""
    update_id: NonNegativeInt = Field(description='Telegram Update ID для дедупликации вопросов и управляющих действий.')
    user_id: PositiveId = Field(description='Telegram User ID сотрудника; не подменяется идентификатором чата.')
    chat_id: PositiveId = Field(description='Положительный ID личного Telegram-чата доставки.')
    received_at: AwareDatetime = Field(description='Первое время приёма вопроса в UTC; повтор его не изменяет.')
    input: QuestionInput = Field(description='Текстовый вопрос либо Telegram file_id изображения с подписью.')


class IntakeResult(DTO):
    """Сохранённый исход приёма или техническая просьба повторить update."""
    kind: Literal['accepted', 'duplicate', 'handled', 'busy', 'denied', 'ignored', 'invalid_update', 'retry_later'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    request_id: PositiveId | None = Field(default=None, description='ID принятого вопроса; не заменяет delivery_id.')
    delivery_id: PositiveId | None = Field(default=None, description='ID самостоятельной доставки в rag_deliveries.')
    case_id: UUID | None = Field(default=None, description='UUID обращения поддержки, уникального для исходного вопроса.')

    @model_validator(mode='after')
    def check_intake(self) -> Self:
        """Принятый вопрос имеет ID; занятость не создаёт новый request."""
        if self.kind == 'accepted' and self.request_id is None:
            raise ValueError('accepted требует request_id')
        if self.kind == 'busy' and self.request_id is not None:
            raise ValueError('busy не содержит ID нового вопроса')
        return self


class SupportAction(DTO):
    """Действие поддержки с привязкой к вопросу или обращению."""
    kind: Literal['offer', 'start', 'confirm', 'decline'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    request_id: PositiveId | None = Field(default=None, description='ID принятого вопроса; не заменяет delivery_id.')
    case_id: UUID | None = Field(default=None, description='UUID обращения поддержки, уникального для исходного вопроса.')
    reason: Literal['dissatisfied', 'human_request'] | None = Field(default=None, description='Типизированная причина обращения или эскалации.')

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
    """Идемпотентная команда поддержки от Telegram-сотрудника."""
    update_id: NonNegativeInt = Field(description='Telegram Update ID для дедупликации вопросов и управляющих действий.')
    user_id: PositiveId = Field(description='Telegram User ID сотрудника; не подменяется идентификатором чата.')
    chat_id: PositiveId = Field(description='Положительный ID личного Telegram-чата доставки.')
    received_at: AwareDatetime = Field(description='Первое время приёма вопроса в UTC; повтор его не изменяет.')
    action: SupportAction = Field(description='Действие поддержки, которое не создаёт новый RAG-вопрос.')


class Principal(DTO):
    """Проверенная личность web-пользователя; роль не принимается из тела запроса."""
    user_id: PositiveId = Field(description='Telegram User ID сотрудника; не подменяется идентификатором чата.')
    role: Literal['employee', 'operator', 'admin'] = Field(description='Минимальная роль интерфейса: employee, operator или admin.')


class AccessRecord(DTO):
    """Допуск сотрудника и минимальная роль интерфейса."""
    user_id: PositiveId = Field(description='Telegram User ID сотрудника; не подменяется идентификатором чата.')
    enabled: bool = Field(description='Допуск новых вопросов; отключение не отменяет уже принятый вопрос.')
    role: Literal['employee', 'operator', 'admin'] = Field(description='Минимальная роль интерфейса: employee, operator или admin.')


class SupportCase(DTO):
    """Ожидание помощи или тикет; один case на исходный request_id."""
    case_id: UUID = Field(description='UUID обращения поддержки, уникального для исходного вопроса.')
    request_id: PositiveId = Field(description='ID принятого вопроса; не заменяет delivery_id.')
    user_id: PositiveId = Field(description='Telegram User ID сотрудника; не подменяется идентификатором чата.')
    state: Literal['waiting', 'awaiting_confirmation', 'creating', 'open', 'closed', 'cancelled'] = Field(description='Сохранённая стадия обращения поддержки.')
    reason: Literal['dissatisfied', 'human_request', 'out_of_scope', 'no_evidence'] = Field(description='Типизированная причина обращения или эскалации.')
    check_at: AwareDatetime | None = Field(default=None, description='Срок ручного ожидания в UTC; повтор не сдвигает таймер.')
    ticket_url: NonEmptyText | None = Field(default=None, description='Реальный адрес созданного тикета, отсутствует до его открытия.')
    created_at: AwareDatetime | None = Field(default=None, description='Фактическое время создания записи в UTC.')
    closed_at: AwareDatetime | None = Field(default=None, description='Время закрытия тикета в UTC; обязательно для closed.')

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
    """Снимок исходного вопроса и фактического ответа для оператора."""
    request_id: PositiveId = Field(description='ID принятого вопроса; не заменяет delivery_id.')
    question_text: NonEmptyText = Field(description='Снимок вопроса для оператора: текст, подпись/OCR или отметка об изображении.')
    answer_text: str | None = Field(default=None, description='Фактический текст ответа; не заполняется выдуманным содержимым при эскалации.')
    outcome: Literal['answered', 'support', 'notice'] = Field(description='Бизнес-исход исходного вопроса, не результат отправки Telegram.')
    received_at: AwareDatetime = Field(description='Первое время приёма вопроса в UTC; повтор его не изменяет.')


class TicketMessage(DTO):
    """Реплика сотрудника или оператора в собственной тикет-системе."""
    message_id: PositiveId = Field(description='ID подтверждённого сообщения Telegram либо реплики тикета по типу DTO.')
    case_id: UUID = Field(description='UUID обращения поддержки, уникального для исходного вопроса.')
    author_id: PositiveId = Field(description='Проверенный идентификатор автора реплики из Principal.')
    author_role: Literal['employee', 'operator', 'admin'] = Field(description='Роль автора реплики на момент её создания.')
    text: NonEmptyText = Field(description='Непустой текст, назначение которого задаёт конкретный DTO.')
    created_at: AwareDatetime = Field(description='Фактическое время создания записи в UTC.')


class TicketView(DTO):
    """Тикет, снимок исходного контекста и дальнейшие реплики."""
    case: SupportCase = Field(description='Обращение с владельцем, стадией и реальной ссылкой при наличии.')
    history: list[HistoryEntry] = Field(description='Снимок исходного вопроса и фактического ответа для оператора.')
    messages: list[TicketMessage] = Field(description='Реплики собственной тикет-системы в сохранённом порядке.')


class MetricsSnapshot(DTO):
    """Наблюдаемые счётчики и длительности за полуоткрытый период."""
    from_at: AwareDatetime = Field(description='Начало полуоткрытого периода статистики в UTC.')
    to_at: AwareDatetime = Field(description='Конец полуоткрытого периода статистики в UTC, не включается.')
    requests: NonNegativeInt = Field(description='Число принятых самостоятельных вопросов за период.')
    answered: NonNegativeInt = Field(description='Число ответов бота по определению метрики; не доказательство решения.')
    escalated: NonNegativeInt = Field(description='Число обращений, переданных в поддержку.')
    dissatisfied: NonNegativeInt = Field(description='Число зафиксированных реакций недовольства.')
    reply_duration_ms: list[NonNegativeInt] = Field(description='Наблюдаемые длительности до первой нормальной текстовой квитанции.')
    ticket_duration_ms: list[NonNegativeInt] = Field(description='Длительности от исходного вопроса до закрытия тикета.')
    topics: dict[NonEmptyText, NonNegativeInt] | None = Field(default=None, description='Распределение тем; None, пока метод разметки не выбран.')

    @model_validator(mode='after')
    def check_period(self) -> Self:
        """Период метрик полуоткрытый и имеет положительную длительность."""
        if self.from_at >= self.to_at:
            raise ValueError('from_at должен предшествовать to_at')
        return self


class InlineAction(DTO):
    """Кнопка с одним назначением: callback либо URL."""
    text: NonEmptyText = Field(description='Видимая подпись кнопки.')
    callback_data: NonEmptyText | None = Field(default=None, description='Команда кнопки, не более 64 байтов UTF-8.')
    url: NonEmptyText | None = Field(default=None, description='Адрес перехода; не используется одновременно с callback_data.')

    @model_validator(mode='after')
    def check_target(self) -> Self:
        """Кнопка имеет одно назначение; callback укладывается в лимит UTF-8."""
        if (self.callback_data is None) == (self.url is None):
            raise ValueError('Нужно ровно одно из callback_data и url')
        if self.callback_data is not None and len(self.callback_data.encode('utf-8')) > 64:
            raise ValueError('callback_data превышает 64 байта')
        return self


class TelegramEntity(DTO):
    """Форматирование видимого текста; offset и length измеряются в UTF-16."""
    type: Literal['bold', 'italic', 'code', 'pre', 'text_link'] = Field(description='Тип entity Telegram: оформление или text_link.')
    offset: NonNegativeInt = Field(description='Начало entity в кодовых единицах UTF-16 видимого текста.')
    length: PositiveId = Field(description='Положительная длина entity в кодовых единицах UTF-16.')
    url: NonEmptyText | None = Field(default=None, description='Адрес перехода; не используется одновременно с callback_data.')

    @model_validator(mode='after')
    def check_link(self) -> Self:
        """URL задаётся только для entity типа text_link и обязателен для неё."""
        if (self.type == 'text_link') != (self.url is not None):
            raise ValueError('URL требуется только для text_link')
        return self


class TextPart(DTO):
    """Одна текстовая отправка с entities и кнопками, без parse_mode."""
    kind: Literal['text'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    text: NonEmptyText = Field(description='Одна отформатированная текстовая часть, не более 4096 единиц UTF-16.')
    entities: list[TelegramEntity] = Field(default_factory=list, description='Entities видимого текста; parse_mode не используется.')
    actions: list[InlineAction] = Field(default_factory=list, description='Кнопки конкретной текстовой части.')

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
    """Одно изображение из сохранённого ответа для отдельной отправки."""
    kind: Literal['image'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    image: ImageRef = Field(description='Разрешённые UUID и описание изображения для отдельной отправки.')


DeliveryPart = Annotated[TextPart | ImagePart, Field(discriminator='kind')]


class ClaimedDelivery(DTO):
    """Доставка с отдельной попыткой, полным планом и сохранённым прогрессом."""
    attempt: DeliveryAttempt = Field(description='Активная попытка, полученная при атомарном взятии работы в PostgreSQL.')
    user_id: PositiveId = Field(description='Telegram User ID сотрудника; не подменяется идентификатором чата.')
    chat_id: PositiveId = Field(description='Положительный ID личного Telegram-чата доставки.')
    request_id: PositiveId | None = Field(default=None, description='ID принятого вопроса; не заменяет delivery_id.')
    purpose: Literal['answer', 'notice', 'status', 'busy', 'denied', 'first_question', 'support_offer', 'support_confirmation', 'ticket_link'] = Field(description='Назначение доставки, определяющее обязательность content/request_id/case.')
    content: RagResult | None = Field(default=None, description='Содержимое, передаваемое следующей операции по контракту.')
    case: SupportCase | None = Field(default=None, description='Обращение с владельцем, стадией и реальной ссылкой при наличии.')
    parts: list[DeliveryPart] | None = Field(default=None, description='Сохранённый план частей; None до первой подготовки.')
    next_part: NonNegativeInt = Field(default=0, description='Индекс первой неподтверждённой части, начиная с 0.')

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
    """Квитанция Telegram: message_id и факт отправки резервного текста."""
    kind: Literal['sent'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    message_id: PositiveId = Field(description='ID подтверждённого сообщения Telegram либо реплики тикета по типу DTO.')
    variant: Literal['normal', 'length_fallback'] = Field(default='normal', description='Обычный текст либо короткий резервный length_fallback.')


class SendFailed(DTO):
    """Отправка не подтверждена; признаки определяют повтор и риск дублирования."""
    kind: Literal['failed'] = Field(description='Метка варианта DTO для выбора исхода при валидации.')
    retryable: bool = Field(description='Можно ли повторить операцию по правилам владельца этапа.')
    uncertain: bool = Field(description='Сообщение могло дойти без подтверждения; повтор способен создать дубль.')
    retry_after_seconds: NonNegativeInt | None = Field(default=None, description='Задержка повтора от Telegram в секундах; None, если не указана.')


SendResult = Annotated[Sent | SendFailed, Field(discriminator='kind')]
