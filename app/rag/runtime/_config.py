"""Process configuration. Reading settings never connects to a service."""

import re
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import SettingsConfigDict
from rag._configuration import ProjectSettings
from sqlalchemy import URL


class RuntimeSettings(ProjectSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_",
        extra="ignore",
        allow_inf_nan=False,
        populate_by_name=True,
        hide_input_in_errors=True,
        env_file_encoding="utf-8-sig",
    )

    pg_host: str = Field(default="localhost", validation_alias="PGHOST")
    pg_port: int = Field(default=5432, validation_alias="PGPORT", gt=0, le=65535)
    pg_database: str = Field(default="rag", validation_alias="PGDATABASE")
    pg_user: str = Field(default="rag", validation_alias="PGUSER")
    pg_password: SecretStr = Field(default=SecretStr(""), validation_alias="PGPASSWORD")
    pg_schema: str = "public"
    pg_pool_size: int = Field(default=10, gt=0)
    io_timeout_s: float = Field(default=10, gt=0)
    question_timeout_s: float = Field(default=120, gt=0)
    delivery_timeout_s: float = Field(default=60, gt=0)
    ingestion_timeout_s: float = Field(default=600, gt=0)
    max_pending_requests: int = Field(default=1000, gt=0)
    recovery_window_s: float = Field(default=60, gt=0)
    redis_url: SecretStr = Field(
        default=SecretStr("redis://localhost:6379/0"), validation_alias="REDIS_URL"
    )
    cache_prefix: str = "rag:exact:v2:"
    cache_ttl_s: int = Field(default=3600, gt=0)
    rabbitmq_host: str = Field(default="localhost", validation_alias="RABBITMQ_HOST")
    rabbitmq_port: int = Field(default=5672, validation_alias="RABBITMQ_PORT", gt=0, le=65535)
    rabbitmq_user: str = Field(default="rag", validation_alias="RABBITMQ_USER")
    rabbitmq_password: SecretStr = Field(
        default=SecretStr(""), validation_alias="RABBITMQ_PASSWORD"
    )
    rabbitmq_vhost: str = "/"
    queue_prefix: str = ""
    broker_retry_s: float = Field(default=1, gt=0, le=30)
    prefetch: int = Field(default=4, gt=0)
    batch_size: int = Field(default=100, gt=0, le=1000)
    qdrant_url: str = Field(default="http://localhost:6333", validation_alias="QDRANT_URL")
    qdrant_api_key: SecretStr | None = None
    chunks_collection: str = "document_chunks"
    topics_collection: str = "topic_questions"
    embedding_profile: str = ""
    embedding_dimensions: int = Field(default=1024, gt=0)
    preparation_profile: str = "ingestion-v1"
    search_mode: str = "hybrid"
    search_candidate_limit: int = Field(default=10, gt=0)
    search_top_k: int = Field(default=3, gt=0)
    search_score_threshold: float | None = None
    embedding_timeout_s: float = Field(default=5, gt=0)
    rerank_timeout_s: float = Field(default=5, gt=0)
    dense_weight: float = Field(default=1, gt=0)
    lexical_weight: float = Field(default=1, gt=0)
    priority_lexical_weight: float = Field(default=2, gt=0)
    rrf_k: int = Field(default=60, gt=0)
    text_language: str = "russian"
    spool_dir: Path = Field(default=Path("/var/lib/rag/spool"), validation_alias="WORKER_SPOOL_DIR")
    max_markdown_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_prepared_bytes: int = Field(default=256 * 1024 * 1024, gt=0)
    max_image_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    s3_endpoint_url: str = Field(
        default="http://localhost:9000", validation_alias="S3_ENDPOINT_URL"
    )
    s3_access_key: SecretStr = Field(default=SecretStr(""), validation_alias="S3_ACCESS_KEY")
    s3_secret_key: SecretStr = Field(default=SecretStr(""), validation_alias="S3_SECRET_KEY")
    s3_region: str = Field(default="us-east-1", validation_alias="S3_REGION")
    s3_bucket: str = "rag-images"
    telegram_bot_token: SecretStr = Field(
        default=SecretStr(""), validation_alias="TELEGRAM_BOT_TOKEN"
    )
    telegram_base_url: str = "https://api.telegram.org"
    admin_login: SecretStr = Field(default=SecretStr(""), validation_alias="ADMIN_LOGIN")
    admin_password: SecretStr = Field(default=SecretStr(""), validation_alias="ADMIN_PASSWORD")
    photo_max_pixels: int = Field(default=25_000_000, gt=0)
    photo_memory_bytes: int = Field(default=512 * 1024 * 1024, ge=128 * 1024 * 1024)
    photo_timeout_s: float = Field(default=15, gt=0)
    relay_interval_s: float = Field(default=1, gt=0)
    recovery_interval_s: float = Field(default=30, gt=0)
    shutdown_timeout_s: float = Field(default=15, gt=0)
    startup_timeout_s: float = Field(default=120, gt=0, lt=86400)
    model_bindings: SecretStr = SecretStr("{}")
    use_reranker: bool = False
    text_only: bool = False
    use_gigachat: bool = False

    @model_validator(mode="after")
    def _answer_cache_namespace(self):
        if self.use_gigachat:
            from rag.model_gateway._gigachat_config import _Options

            suffix = _Options()._profile() + ":"
            if not self.cache_prefix.endswith(suffix):
                self.cache_prefix += suffix
        return self

    @field_validator("pg_schema")
    @classmethod
    def _schema(cls, value):
        if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", value):
            raise ValueError("Invalid PostgreSQL schema identifier")
        return value

    @field_validator("search_mode")
    @classmethod
    def _mode(cls, value):
        if value not in ("dense", "hybrid"):
            raise ValueError("search_mode must be dense or hybrid")
        return value

    @field_validator("s3_endpoint_url", "telegram_base_url", "qdrant_url")
    @classmethod
    def _http_url(cls, value):
        parsed = urlsplit(value)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Expected an HTTP endpoint without credentials, query or fragment")
        return value

    @field_validator("s3_region")
    @classmethod
    def _region(cls, value):
        if not re.fullmatch(r"[a-z0-9-]{1,63}", value):
            raise ValueError("Invalid S3 region")
        return value

    @field_validator("s3_bucket")
    @classmethod
    def _bucket(cls, value):
        if not re.fullmatch(r"[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]", value) or ".." in value:
            raise ValueError("Invalid S3 bucket name")
        return value

    @model_validator(mode="after")
    def _search_profile(self):
        if self.search_top_k > self.search_candidate_limit:
            raise ValueError("Search top_k must not exceed the candidate limit")
        if self.priority_lexical_weight <= max(self.dense_weight, self.lexical_weight):
            raise ValueError(
                "Priority lexical weight must exceed the dense and regular lexical weights"
            )
        if self.chunks_collection == self.topics_collection:
            raise ValueError("Chunks and topics require distinct collections")
        return self

    def _database_url(self):
        return URL.create(
            "postgresql+asyncpg",
            username=self.pg_user,
            password=self.pg_password.get_secret_value(),
            host=self.pg_host,
            port=self.pg_port,
            database=self.pg_database,
        )
