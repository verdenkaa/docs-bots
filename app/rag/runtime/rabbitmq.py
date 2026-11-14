"""Contracts: docs/documents/infrastructure/rabbitmq/contracts.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field

from rag.contracts import DeliveryRef, RequestRef
from rag.dto import DTO
from rag.ingestion.contracts import JobRef


WorkRoute = Literal['rag.questions', 'rag.results', 'ingestion.jobs']


class QuestionNotice(DTO):
    """Уведомление rag.questions со ссылкой на принятый вопрос."""
    message_id: UUID = Field(description='ID подтверждённого сообщения Telegram либо реплики тикета по типу DTO.')
    route: Literal['rag.questions'] = Field(description='Маршрут RabbitMQ, который определяет тип event и обработчик.')
    event: RequestRef = Field(description='Типизированная ссылка на сохранённую работу; без текста и файлов.')


class ResultNotice(DTO):
    """Уведомление rag.results со ссылкой на самостоятельную доставку."""
    message_id: UUID = Field(description='ID подтверждённого сообщения Telegram либо реплики тикета по типу DTO.')
    route: Literal['rag.results'] = Field(description='Маршрут RabbitMQ, который определяет тип event и обработчик.')
    event: DeliveryRef = Field(description='Типизированная ссылка на сохранённую работу; без текста и файлов.')


class IngestionNotice(DTO):
    """Уведомление о задании индексации для последовательного потребителя Worker."""
    message_id: UUID = Field(description='ID подтверждённого сообщения Telegram либо реплики тикета по типу DTO.')
    route: Literal['ingestion.jobs'] = Field(description='Ключ маршрутизации RabbitMQ, определяющий потребителя и тип event.')
    event: JobRef = Field(description='Ссылка на сохранённую работу; без полного вопроса, ответа или файла.')


OutboxNotice = Annotated[QuestionNotice | ResultNotice | IngestionNotice, Field(discriminator='route')]


class BrokerPublishError(Exception):
    """Брокер не подтвердил маршрутизированную публикацию.

    Повтор может дать дубль: потребитель проверяет состояние работы в PostgreSQL.
    """


async def publish_notice(notice: OutboxNotice) -> None:
    """Опубликовать уведомление с publisher confirm и проверкой маршрутизации.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        notice: Вход по контракту владельца.

    Returns:
        None
    """
    raise NotImplementedError
