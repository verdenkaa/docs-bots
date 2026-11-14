"""Technical budgets, loader paths and transport limits; no model defaults."""

from pathlib import Path
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class _Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MODEL_GATEWAY_",
        extra="ignore",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        env_file=".env",
        env_file_encoding="utf-8",
    )
    queue_size: int = Field(default=32, ge=0)
    startup_timeout_s: float = Field(default=120, gt=0)
    shutdown_timeout_s: float = Field(default=15, gt=0)
    stop_grace_s: float = Field(default=1, gt=0, le=10)
    max_batch_items: int = Field(default=128, gt=0)
    max_text_chars: int = Field(default=1_000_000, gt=0)
    max_image_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    max_message_bytes: int = Field(default=64 * 1024 * 1024, gt=0, lt=2**32)
    provider_modules: list[str] = Field(default_factory=list)
    local_loaders: dict[str, str] = Field(default_factory=dict)
    cache_dir: Path = Path("/var/lib/rag/models")
    local_memory_bytes: int | None = Field(default=None, gt=0)
    max_local_workers: int = Field(default=4, gt=0)
    cache_size: int = Field(default=256, ge=0)
    cache_ttl_s: float = Field(default=60, gt=0)
    coordination_poll_s: float = Field(default=0.1, gt=0)
