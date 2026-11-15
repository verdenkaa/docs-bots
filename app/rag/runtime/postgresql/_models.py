"""Persistence models. Public DTOs remain independent of SQLAlchemy and sessions.

Alembic owns DDL; importing this module never creates tables or connections.
"""

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Index,
    JSON,
    SmallInteger,
    String,
    Text,
    Uuid,
    UniqueConstraint,
    func,
    literal_column,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class _Base(DeclarativeBase):
    pass


_json_type = JSON(none_as_null=True).with_variant(JSONB(none_as_null=True), "postgresql")


class _Employee(_Base):
    __tablename__ = "employees"
    __table_args__ = (
        CheckConstraint("username ~ '^[a-z0-9_]{1,32}$'", name="employees_username_format"),
    )
    username: Mapped[str] = mapped_column(Text, primary_key=True)
    enabled: Mapped[bool] = mapped_column(Boolean)


class _Request(_Base):
    __tablename__ = "rag_requests"
    __table_args__ = (
        CheckConstraint("state IN ('queued','processing','awaiting_delivery','awaiting_support','finished')", name="rag_requests_state_check"),
        CheckConstraint("pinned_generation>=0", name="rag_requests_pinned_generation_check"),
        CheckConstraint("attempt_no>=0", name="rag_requests_attempt_no_check"),
        CheckConstraint("username IS NULL OR username ~ '^[a-z0-9_]{1,32}$'", name="rag_requests_username_format"),
        Index("one_active_request", "user_id", unique=True, postgresql_where=literal_column("state!='finished'")),
        Index("requests_recovery", "state", "resume_at"),
        Index("requests_generation", "pinned_generation", postgresql_where=literal_column("pinned_generation IS NOT NULL")),
        Index("requests_received", "received_at"),
    )
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
    __table_args__ = (CheckConstraint("update_id>=0", name="telegram_updates_update_id_check"),)
    update_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=False)
    user_id: Mapped[int | None] = mapped_column(BigInteger)
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    action: Mapped[dict[str, Any] | None] = mapped_column(_json_type)
    result: Mapped[dict[str, Any]] = mapped_column(_json_type)


class _SupportCase(_Base):
    __tablename__ = "support_cases"
    __table_args__ = (
        CheckConstraint("state IN ('waiting','awaiting_confirmation','creating','open','closed','cancelled')", name="support_cases_state_check"),
        CheckConstraint("reason IN ('dissatisfied','human_request','out_of_scope','no_evidence')", name="support_cases_reason_check"),
        CheckConstraint("(state IN ('open','closed')) = (ticket_url IS NOT NULL AND created_at IS NOT NULL)", name="support_cases_ticket_check"),
        CheckConstraint("(state='closed') = (closed_at IS NOT NULL)", name="support_cases_closed_check"),
        Index("support_due", "state", "check_at"),
    )
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
    __table_args__ = (
        CheckConstraint("purpose IN ('answer','notice','status','busy','denied','first_question','support_offer','support_confirmation','ticket_link')", name="rag_deliveries_purpose_check"),
        CheckConstraint("next_part>=0", name="rag_deliveries_next_part_check"),
        CheckConstraint("state IN ('waiting','sending','delivered','failed','unknown','cancelled')", name="rag_deliveries_state_check"),
        CheckConstraint("attempt_no>=0", name="rag_deliveries_attempt_no_check"),
        Index("one_terminal_delivery", "request_id", unique=True, postgresql_where=literal_column("terminal_for_request")),
        Index("one_wait_status", "request_id", unique=True, postgresql_where=literal_column("purpose='status'")),
        Index("delivery_order", "chat_id", "delivery_id", postgresql_where=literal_column("state IN ('waiting','sending')")),
        Index("delivery_recovery", "state", "resume_at"),
    )
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
    __table_args__ = (
        UniqueConstraint("case_id", "author_role", "client_message_id", name="support_messages_case_role_client_key"),
        CheckConstraint("length(btrim(text))>0", name="support_messages_text_check"),
        CheckConstraint("author_role IN ('visitor','operator') AND ((author_role='operator') = (author_login IS NOT NULL))", name="support_messages_public_author"),
    )
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    case_id: Mapped[UUID] = mapped_column(ForeignKey("support_cases.case_id"))
    author_role: Mapped[str] = mapped_column(Text)
    author_login: Mapped[str | None] = mapped_column(Text)
    client_message_id: Mapped[UUID] = mapped_column(Uuid)
    text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.clock_timestamp()
    )


class _Document(_Base):
    __tablename__ = "documents"
    __table_args__ = (
        CheckConstraint("length(btrim(source_name))>0", name="documents_source_name_nonempty"),
        CheckConstraint("current_version IS NULL OR current_version>=0", name="documents_version_nonnegative"),
        CheckConstraint("(current_revision_id IS NULL)=(current_version IS NULL)", name="documents_current_pair"),
    )
    document_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    source_name: Mapped[str] = mapped_column(Text(collation="C"), unique=True)
    current_revision_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("document_revisions.revision_id", use_alter=True, name="documents_current_revision_fk")
    )
    current_version: Mapped[int | None] = mapped_column(Integer)


class _Revision(_Base):
    __tablename__ = "document_revisions"
    __table_args__ = (
        UniqueConstraint("document_id", "revision_no", name="document_revisions_document_id_revision_no_key"),
        CheckConstraint("revision_no>0", name="document_revisions_revision_no_check"),
        CheckConstraint("length(btrim(source_name))>0", name="document_revisions_source_name_check"),
        CheckConstraint("state IN ('queued','indexing','published','retired','failed','deleting','cleaned')", name="document_revisions_state_check"),
        CheckConstraint("attempt_no>=0", name="document_revisions_attempt_no_check"),
        CheckConstraint("expected_chunk_count>0", name="document_revisions_expected_chunk_count_check"),
        CheckConstraint("expected_topic_count>0", name="document_revisions_expected_topic_count_check"),
        CheckConstraint("(expected_chunk_count IS NULL)=(expected_topic_count IS NULL)", name="document_revisions_counts_check"),
        CheckConstraint("visible_until IS NULL OR visible_until>visible_from", name="document_revisions_visibility_check"),
        CheckConstraint("length(btrim(access_level))>0", name="revisions_access_nonempty"),
        CheckConstraint("(document_version IS NULL AND base_version IS NULL) OR (document_version IS NOT NULL AND document_version>=0 AND ((base_version IS NULL AND document_version=0) OR (base_version IS NOT NULL AND base_version>=0 AND document_version=base_version+1)))", name="revisions_reserved_version"),
        CheckConstraint("expected_chunk_count IS NULL OR document_version IS NOT NULL", name="revisions_counts_require_version"),
        Index("one_published_revision", "document_id", unique=True, postgresql_where=literal_column("state='published'")),
        Index("revisions_recovery", "state", "resume_at"),
        Index("revisions_visibility", "visible_from", "visible_until"),
        Index("revisions_published_version", "document_id", "document_version", unique=True, postgresql_where=literal_column("visible_from IS NOT NULL")),
    )
    revision_id: Mapped[UUID] = mapped_column(Uuid, primary_key=True)
    document_id: Mapped[UUID] = mapped_column(ForeignKey("documents.document_id"))
    revision_no: Mapped[int] = mapped_column(Integer)
    source_name: Mapped[str] = mapped_column(Text)
    access_level: Mapped[str] = mapped_column(Text)
    document_version: Mapped[int | None] = mapped_column(Integer)
    base_version: Mapped[int | None] = mapped_column(Integer)
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
    __table_args__ = (
        CheckConstraint("singleton_id=1", name="corpus_state_singleton_id_check"),
        CheckConstraint("generation>=0", name="corpus_state_generation_check"),
    )
    singleton_id: Mapped[int] = mapped_column(SmallInteger, primary_key=True, autoincrement=False)
    generation: Mapped[int] = mapped_column(BigInteger)
    embedding_profile: Mapped[str | None] = mapped_column(Text)
    embedding_dimensions: Mapped[int | None] = mapped_column(Integer)


class _Outbox(_Base):
    __tablename__ = "outbox"
    __table_args__ = (
        CheckConstraint("(route='rag.questions' AND request_id IS NOT NULL AND delivery_id IS NULL AND revision_id IS NULL) OR (route='rag.results' AND request_id IS NULL AND delivery_id IS NOT NULL AND revision_id IS NULL) OR (route='ingestion.jobs' AND request_id IS NULL AND delivery_id IS NULL AND revision_id IS NOT NULL)", name="outbox_route_check"),
        Index("outbox_ready", "available_at", postgresql_where=literal_column("published_at IS NULL")),
        Index("outbox_questions", "request_id", "route", "published_at"),
        Index("outbox_deliveries", "delivery_id", "route", "published_at"),
        Index("outbox_revisions", "revision_id", "route", "published_at"),
    )
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


class _ModelCall(_Base):
    __tablename__ = "model_calls"
    __table_args__ = (
        CheckConstraint("state IN ('executing','completed','uncertain')", name="model_calls_state"),
        CheckConstraint("(state='completed')=(result IS NOT NULL)", name="model_calls_result"),
        Index("model_calls_pending", "state", "created_at"),
    )
    call_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    owner_id: Mapped[UUID] = mapped_column(Uuid)
    owner_pid: Mapped[int] = mapped_column(Integer)
    owner_host: Mapped[str] = mapped_column(Text)
    work_ref: Mapped[str] = mapped_column(Text)
    profile_id: Mapped[str] = mapped_column(Text)
    model_role: Mapped[str] = mapped_column(Text)
    operation: Mapped[str] = mapped_column(Text)
    state: Mapped[str] = mapped_column(Text)
    result: Mapped[dict[str, Any] | None] = mapped_column(_json_type)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
