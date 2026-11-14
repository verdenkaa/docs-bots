"""Persistence models. Public DTOs remain independent of SQLAlchemy and sessions.

Alembic owns DDL; importing this module never creates tables or connections.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    JSON,
    SmallInteger,
    String,
    Text,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _Base(DeclarativeBase):
    pass


_json_type = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class _Employee(_Base):
    __tablename__ = "employees"
    username: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)


class _Request(_Base):
    __tablename__ = "rag_requests"
    request_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    username: Mapped[str | None] = mapped_column(Text)
    update_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    input: Mapped[dict[str, Any]] = mapped_column(_json_type)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    prepared_text: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="queued")
    pinned_generation: Mapped[int | None] = mapped_column(BigInteger)
    result: Mapped[dict[str, Any] | None] = mapped_column(_json_type)
    attempt_no: Mapped[int] = mapped_column(Integer, server_default="0")
    resume_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class _TelegramUpdate(_Base):
    __tablename__ = "telegram_updates"
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action: Mapped[dict[str, Any] | None] = mapped_column(_json_type)
    result: Mapped[dict[str, Any]] = mapped_column(_json_type)


class _SupportCase(_Base):
    __tablename__ = "support_cases"
    case_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    request_id: Mapped[int] = mapped_column(ForeignKey("rag_requests.request_id"), unique=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    state: Mapped[str] = mapped_column(Text)
    reason: Mapped[str] = mapped_column(Text)
    check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ticket_url: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    history: Mapped[list[dict[str, Any]]] = mapped_column(_json_type, server_default="[]")


class _Delivery(_Base):
    __tablename__ = "rag_deliveries"
    delivery_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    chat_id: Mapped[int] = mapped_column(BigInteger)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("rag_requests.request_id"))
    case_id: Mapped[UUID | None] = mapped_column(ForeignKey("support_cases.case_id"))
    purpose: Mapped[str] = mapped_column(Text)
    content: Mapped[dict[str, Any] | None] = mapped_column(_json_type)
    terminal_for_request: Mapped[bool] = mapped_column(Boolean, server_default="false")
    dedupe_key: Mapped[str | None] = mapped_column(Text, unique=True)
    parts: Mapped[list[dict[str, Any]] | None] = mapped_column(_json_type)
    receipts: Mapped[list[dict[str, Any]]] = mapped_column(_json_type, server_default="[]")
    next_part: Mapped[int] = mapped_column(Integer, server_default="0")
    state: Mapped[str] = mapped_column(Text, server_default="waiting")
    attempt_no: Mapped[int] = mapped_column(Integer, server_default="0")
    resume_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivery_uncertain: Mapped[bool] = mapped_column(Boolean, server_default="false")


class _SupportMessage(_Base):
    __tablename__ = "support_messages"
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("support_cases.case_id"))
    author_role: Mapped[str] = mapped_column(Text)
    author_login: Mapped[str | None] = mapped_column(Text)
    client_message_id: Mapped[UUID] = mapped_column(Uuid)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.current_timestamp()
    )
    legacy_author_id: Mapped[int | None] = mapped_column(BigInteger)
    legacy_author_role: Mapped[str | None] = mapped_column(Text)
    legacy_client_message_id: Mapped[UUID | None] = mapped_column(Uuid)


class _Revision(_Base):
    __tablename__ = "document_revisions"
    revision_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    document_id: Mapped[UUID] = mapped_column(Uuid)
    revision_no: Mapped[int] = mapped_column(Integer)
    source_name: Mapped[str] = mapped_column(Text)
    image_ids: Mapped[list[str]] = mapped_column(_json_type, server_default="[]")
    staging_ref: Mapped[str | None] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text, server_default="queued")
    attempt_no: Mapped[int] = mapped_column(Integer, server_default="0")
    resume_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    qdrant_write_pending: Mapped[bool] = mapped_column(Boolean, server_default="false")
    expected_chunk_count: Mapped[int | None] = mapped_column(Integer)
    expected_topic_count: Mapped[int | None] = mapped_column(Integer)
    visible_from: Mapped[int | None] = mapped_column(BigInteger)
    visible_until: Mapped[int | None] = mapped_column(BigInteger)


class _CorpusState(_Base):
    __tablename__ = "corpus_state"
    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    generation: Mapped[int] = mapped_column(BigInteger)
    embedding_profile: Mapped[str | None] = mapped_column(Text)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)


class _Outbox(_Base):
    __tablename__ = "outbox"
    message_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    route: Mapped[str] = mapped_column(Text)
    request_id: Mapped[int | None] = mapped_column(ForeignKey("rag_requests.request_id"))
    delivery_id: Mapped[int | None] = mapped_column(ForeignKey("rag_deliveries.delivery_id"))
    revision_id: Mapped[UUID | None] = mapped_column(ForeignKey("document_revisions.revision_id"))
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class _SchemaVersion(_Base):
    __tablename__ = "alembic_version"
    version_num: Mapped[str] = mapped_column(String(32), primary_key=True)
