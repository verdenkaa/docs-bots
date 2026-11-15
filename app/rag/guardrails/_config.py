"""Настройки Guardrails. Чтение настроек не запускает процессы и не обращается к сервисам.

Значения по умолчанию — **предположения, не решения команды**: пороги и лимиты в документации
не названы (docs/documents/guardrails/detector.md требует подбора по размеченным примерам).
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import SettingsConfigDict
from rag._configuration import ProjectSettings


class GuardrailsSettings(ProjectSettings):
    """Переменные окружения RAG_GUARDRAILS_*."""

    model_config = SettingsConfigDict(
        env_prefix="RAG_GUARDRAILS_",
        extra="ignore",
        allow_inf_nan=False,
        hide_input_in_errors=True,
        env_file_encoding="utf-8-sig",
    )

    max_question_chars: int = Field(default=4000, gt=0)  # длиннее — invalid_input
    detector_timeout_s: float = Field(default=5, gt=0)
    embedding_timeout_s: float = Field(default=5, gt=0)
    topic_timeout_s: float = Field(default=5, gt=0)  # у find_topic_matches нет своего бюджета
    # ниже порога косинусного сходства с ближайшим примером — out_of_scope; порог щадящий
    topic_threshold: float = Field(default=0.3, ge=-1, le=1)
    # правило для профилей, которые возвращают только score: profile_id -> порог, при котором это инъекция
    injection_score_thresholds: dict[str, float] = Field(default_factory=dict)


@lru_cache(maxsize=1)
def guardrails_settings() -> GuardrailsSettings:
    return GuardrailsSettings()
