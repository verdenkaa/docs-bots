"""Contracts: docs/documents/infrastructure/rabbitmq/contracts.md."""

from pydantic import Field
from rag.dto import DTO


from typing import Annotated, Literal
from uuid import UUID

from rag.contracts import RequestRef
from rag.ingestion.contracts import JobRef


WorkRoute = Literal["rag.questions", "rag.results", "ingestion.jobs"]


class RagNotice(DTO):
    """Уведомление о вопросе или готовом ответе; данные работы остаются в PostgreSQL."""

    message_id: UUID = Field(description="Стабильный UUID записи outbox, используемый как AMQP message_id.")
    route: Literal["rag.questions", "rag.results"] = Field(
        description="Ключ маршрутизации RabbitMQ, определяющий потребителя и тип event.",
    )
    event: RequestRef = Field(description="Ссылка на сохранённую работу; без полного вопроса, ответа или файла.")


class IngestionNotice(DTO):
    """Уведомление о задании индексации для последовательного потребителя Worker."""

    message_id: UUID = Field(description="Стабильный UUID записи outbox, используемый как AMQP message_id.")
    route: Literal["ingestion.jobs"] = Field(
        description="Ключ маршрутизации RabbitMQ, определяющий потребителя и тип event.",
    )
    event: JobRef = Field(description="Ссылка на сохранённую работу; без полного вопроса, ответа или файла.")


OutboxNotice = Annotated[RagNotice | IngestionNotice, Field(discriminator="route")]


class BrokerPublishError(Exception):
    """Брокер не подтвердил маршрутизированную публикацию.

    Повтор может дать дубль: потребитель проверяет состояние работы в PostgreSQL.
    """


async def publish_notice(notice: OutboxNotice) -> None:
    """Опубликовать уведомление с publisher confirm и проверкой маршрутизации.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        notice: Уведомление из поля notice записи ReadyOutboxRow.

    Returns:
        None после подтверждённой маршрутизированной публикации.
        Иначе BrokerPublishError; наличие сообщения при неизвестном исходе не исключено.
    """
    raise NotImplementedError
