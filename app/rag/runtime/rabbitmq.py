"""Contracts: docs/documents/infrastructure/rabbitmq/contracts.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID, uuid4

from pydantic import Field

from rag.contracts import DeliveryRef, RequestRef
from rag.dto import DTO
from rag.ingestion.contracts import JobRef

import asyncio
import aio_pika
from aio_pika import Message, DeliveryMode
from aio_pika.exceptions import AMQPException
from pamqp.commands import Basic
from pydantic import TypeAdapter, ValidationError
from rag.runtime._state import _get
from rag.runtime.events import emit_event
from rag.runtime import _watchdog


async def _connect_broker():
    state = _get()
    async with state.broker_lock:
        if state.broker is not None and not state.broker.is_closed:
            return
        settings = state.settings
        broker = await aio_pika.connect_robust(host=settings.rabbitmq_host,
            port=settings.rabbitmq_port, login=settings.rabbitmq_user,
            password=settings.rabbitmq_password.get_secret_value(), virtualhost=settings.rabbitmq_vhost,
            timeout=settings.io_timeout_s)
        try:
            channel = await broker.channel(publisher_confirms=True, on_return_raises=True)
            for route in ("rag.questions", "rag.results", "ingestion.jobs"):
                await channel.declare_queue(settings.queue_prefix + route, durable=True)
        except BaseException:
            await broker.close()
            raise
        state.broker, state.channel = broker, channel


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

    Args:
        notice: Вход по контракту владельца.

    Returns:
        None
    """
    notice = TypeAdapter(OutboxNotice).validate_json(notice.model_dump_json())
    state = _get()
    try:
        async with asyncio.timeout(state.settings.io_timeout_s):
            await _connect_broker()
            confirmation = await state.channel.default_exchange.publish(
                Message(body=notice.event.model_dump_json().encode("utf-8"),message_id=str(notice.message_id),
                        content_type="application/json",delivery_mode=DeliveryMode.PERSISTENT),
                routing_key=state.settings.queue_prefix+notice.route,mandatory=True)
            if not isinstance(confirmation,Basic.Ack):
                raise BrokerPublishError("Broker did not acknowledge publication")
    except (AMQPException,OSError,TimeoutError) as exc:
        raise BrokerPublishError("Routed publication was not confirmed") from exc


async def _consume(route, handler, stop):
    """Bounded manual acknowledgements; storage failures remain broker retries."""
    state = _get()
    await _connect_broker()
    annotation = {"rag.questions":RequestRef,"rag.results":DeliveryRef,"ingestion.jobs":JobRef}[route]
    adapter = TypeAdapter(annotation)
    channel = await state.broker.channel()
    await channel.set_qos(prefetch_count=1 if route=="ingestion.jobs" else state.settings.prefetch)
    queue = await channel.declare_queue(state.settings.queue_prefix+route,durable=True)
    active = set()

    async def receive(message):
        task = asyncio.current_task()
        active.add(task)
        run_id = str(uuid4())
        try:
            try:
                UUID(message.message_id or "")
                if message.content_type != "application/json":
                    raise ValueError("Expected JSON")
                event = adapter.validate_json(message.body)
            except (ValidationError,ValueError,TypeError):
                emit_event("rabbitmq","consume","finished",run_id,route=route,outcome="invalid_event")
                await message.ack()
                return
            try:
                if route == "ingestion.jobs":
                    _watchdog._begin(state.settings.io_timeout_s, event.job_id)
                decision = await handler(event)
                if decision not in ("ack","retry_broker"):
                    raise ValueError("Invalid ConsumeDecision")
            except Exception:
                # Handler state is unknown; do not expose exception text or body.
                emit_event("rabbitmq","consume","finished",run_id,route=route,outcome="handler_failed")
                decision = "retry_broker"
            finally:
                if route == "ingestion.jobs":
                    _watchdog._end()
            if decision == "ack":
                await message.ack()
            else:
                await asyncio.sleep(state.settings.broker_retry_s)
                await message.nack(requeue=True)
        finally:
            active.discard(task)

    try:
        if route == "ingestion.jobs":
            _watchdog._send(None, "idle")
        tag = await queue.consume(receive,no_ack=False)
        await stop.wait()
        await queue.cancel(tag)
    finally:
        if active:
            _, pending = await asyncio.wait(active,timeout=state.settings.shutdown_timeout_s)
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending,return_exceptions=True)
        await channel.close()
