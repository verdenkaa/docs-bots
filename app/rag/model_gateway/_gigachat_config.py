"""Private configuration for the opt-in answer backend; no network on import."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

from pydantic import Field, field_validator
from pydantic_settings import SettingsConfigDict

from rag._configuration import ProjectSettings
from rag.contracts import StepError
from rag.model_gateway.contracts import ModelBinding


class _Options(ProjectSettings):
    model_config = SettingsConfigDict(env_prefix="GIGACHAT_", extra="ignore",
                                      hide_input_in_errors=True)

    model: str = ""
    endpoint: str = "https://api.giga.chat/v1"
    auth_url: str = "https://ngw.devices.sberbank.ru:9443/api/v2/oauth"
    scope: str = "GIGACHAT_API_PERS"
    ca_bundle: Path | None = None
    verify_ssl_certs: bool = True
    max_concurrency: int = Field(default=1, gt=0)

    @field_validator("endpoint", "auth_url")
    @classmethod
    def _https(cls, value):
        parsed = urlsplit(value)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment):
            raise ValueError("GigaChat requires HTTPS URLs without credentials or query")
        return value.rstrip("/")

    @field_validator("scope")
    @classmethod
    def _scope(cls, value):
        if value not in {"GIGACHAT_API_PERS", "GIGACHAT_API_B2B", "GIGACHAT_API_CORP"}:
            raise ValueError("Unsupported GigaChat scope")
        return value

    def _profile(self):
        if not self.model.strip():
            raise StepError("model_configuration_error", False)
        fingerprint = hashlib.sha256(json.dumps(
            [self.model, self.endpoint, self.scope, "answer-json-object-v2"],
            ensure_ascii=True, separators=(",", ":"),
        ).encode()).hexdigest()[:20]
        return "gigachat-answer-v2-" + fingerprint

    def _binding(self):
        return ModelBinding(
            profile_id=self._profile(), execution="remote", provider="gigachat",
            model_id=self.model, model_revision=self.model, endpoint=self.endpoint,
            credential_env="GIGACHAT_CREDENTIALS", max_concurrency=self.max_concurrency,
            embedding_dimensions=None,
        )
