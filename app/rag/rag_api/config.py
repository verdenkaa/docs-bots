"""Lazy settings; no clients or environment mutation on import."""
import re
from urllib.parse import urlsplit
from pydantic import AliasChoices, Field, SecretStr
from pydantic_settings import SettingsConfigDict
from rag._configuration import ProjectSettings
from rag.runtime._config import RuntimeSettings

TELEGRAM_INTAKE_PATH = "/internal/telegram/updates"


class _ApiConfig(ProjectSettings):
    model_config = SettingsConfigDict(extra='ignore', hide_input_in_errors=True)
    secret: SecretStr = Field(default=SecretStr(''), validation_alias=AliasChoices('TELEGRAM_INTAKE_SECRET', 'TELEGRAM_WEBHOOK_SECRET'))
    public_url: str = Field(default='', validation_alias=AliasChoices('RAG_PUBLIC_URL', 'TELEGRAM_WEBHOOK_URL'))


def trusted_base_url(value: str) -> str:
    parsed = urlsplit(value)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or parsed.query or parsed.fragment
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("Invalid public HTTPS base URL")
    try:
        parsed.port
    except ValueError:
        raise ValueError("Invalid public HTTPS port") from None
    return value.rstrip("/")


class _ApiSettings:
    # Keep secrets out of repr and error messages without adding a transport DTO.
    def __init__(self, token, public_url, telegram_base_url, timeout_s):
        self.token = token
        self.public_url = public_url
        self.telegram_base_url = telegram_base_url
        self.timeout_s = timeout_s

    @property
    def intake_secret(self):
        secret = _ApiConfig().secret.get_secret_value()
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,256}", secret):
            raise ValueError("Invalid TELEGRAM_INTAKE_SECRET")
        return secret

    @property
    def telegram_url(self):
        return self.telegram_base_url.rstrip("/") + "/bot" + self.token


def get_settings():
    runtime = RuntimeSettings()
    api = _ApiConfig()
    token = runtime.telegram_bot_token.get_secret_value()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
        raise ValueError("Invalid TELEGRAM_BOT_TOKEN")
    return _ApiSettings(token,
                       trusted_base_url(api.public_url),
                       runtime.telegram_base_url, runtime.io_timeout_s)
