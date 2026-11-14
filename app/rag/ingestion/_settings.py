"""Private Worker policy; no model, client or environment is loaded on import."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from rag.contracts import StepError
from rag.dto import NonEmptyText, PositiveId


class _Settings(BaseModel):
    """Model-specific limits are required and must belong to a tested profile.

    Character limits are deployment constraints, not token counts. The profile
    owner must choose them to fit the provider's input context and tokenizer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    embedding_profile: NonEmptyText
    generation_profile: NonEmptyText
    preparation_profile: NonEmptyText
    embedding_dimensions: PositiveId
    embedding_input_characters: PositiveId
    topic_input_characters: PositiveId
    generation_max_output_tokens: PositiveId

    max_attempts: PositiveId = 3
    step_timeout_s: float = Field(default=10.0, gt=0)
    embedding_timeout_s: float = Field(default=30.0, gt=0)
    generation_timeout_s: float = Field(default=60.0, gt=0)
    index_timeout_s: float = Field(default=30.0, gt=0)
    retry_initial_s: float = Field(default=1.0, gt=0)
    retry_max_s: float = Field(default=60.0, gt=0)
    embedding_batch_size: PositiveId = 32
    topic_questions_per_portion: PositiveId = 8
    max_topic_questions: PositiveId = 10_000
    max_question_characters: PositiveId = 1_000
    max_response_characters: PositiveId = 262_144
    max_document_characters: PositiveId = 2_000_000
    max_chunks: PositiveId = 10_000
    cleanup_limit: PositiveId = 20
    cleanup_timeout_s: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def _check_limits(self) -> _Settings:
        if self.retry_initial_s > self.retry_max_s:
            raise ValueError("retry_initial_s exceeds retry_max_s")
        if self.topic_questions_per_portion > self.max_topic_questions:
            raise ValueError("per-portion question limit exceeds the total limit")
        return self

    @classmethod
    def from_environment(cls) -> _Settings:
        try:
            values = {name: os.environ["INGESTION_" + name.upper()]
                      for name in cls.model_fields
                      if "INGESTION_" + name.upper() in os.environ}
            return cls.model_validate(values)
        except ValidationError:
            raise StepError("ingestion_configuration_error", False) from None
