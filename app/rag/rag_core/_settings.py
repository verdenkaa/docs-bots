"""Core policy loaded at call time; no services at import."""
from pydantic import Field
from pydantic_settings import SettingsConfigDict
from rag._configuration import ProjectSettings

class _Settings(ProjectSettings):
    model_config = SettingsConfigDict(env_prefix="RAG_CORE_", extra="ignore", allow_inf_nan=False)
    cache_timeout_s: float = Field(default=0.25, gt=0)
    generation_timeout_s: float = Field(default=60, gt=0)
    ocr_timeout_s: float = Field(default=30, gt=0)
    max_input_bytes: int = Field(default=12000, gt=0)
    max_output_tokens: int = Field(default=1024, gt=0)
    temperature: float = Field(default=0, ge=0)
    max_attempts: int = Field(default=3, gt=0)
    retry_delay_s: float = Field(default=5, gt=0)
