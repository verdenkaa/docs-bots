"""Private settings for upload admission; no resources are opened on import."""

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class _GatewaySettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="RAG_INGESTION_", env_file=".env", env_file_encoding="utf-8",
        extra="ignore", hide_input_in_errors=True, allow_inf_nan=False,
    )

    token: SecretStr = SecretStr("")
    max_upload_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    upload_timeout_s: float = Field(default=600, gt=0)
