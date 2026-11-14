"""Лимиты Document Converter. Чтение настроек не запускает процессы и не загружает модели."""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class ConverterSettings(BaseSettings):
    """Переменные окружения RAG_CONVERTER_*; значения по умолчанию — для документов кейса."""

    model_config = SettingsConfigDict(
        env_prefix="RAG_CONVERTER_",
        extra="ignore",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        env_file=".env",
        env_file_encoding="utf-8",
    )

    max_upload_bytes: int = Field(default=50 * 1024 * 1024, gt=0)
    max_images: int = Field(default=500, gt=0)
    max_image_bytes: int = Field(default=20 * 1024 * 1024, gt=0)  # не больше RAG_MAX_IMAGE_BYTES у Runtime
    max_description_chars: int = Field(default=2000, gt=0)
    total_timeout_s: float = Field(default=1800, gt=0)  # весь convert_document вместе с OCR/VLM
    parse_timeout_s: float = Field(default=300, gt=0)  # только процесс разбора
    parse_memory_bytes: int = Field(default=4 * 1024**3, ge=256 * 1024 * 1024)
    ocr_timeout_s: float = Field(default=30, gt=0)
    describe_timeout_s: float = Field(default=60, gt=0)
    model_concurrency: int = Field(default=4, gt=0)


@lru_cache(maxsize=1)
def converter_settings() -> ConverterSettings:
    return ConverterSettings()
