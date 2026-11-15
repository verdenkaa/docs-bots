"""One lazy configuration source for CLI, ASGI and installed containers."""

import os
from pathlib import Path

from dotenv.main import DotEnv
from pydantic_settings import BaseSettings, SettingsConfigDict


_DEFAULT_ENV_FILE = Path(__file__).resolve().parents[2] / ".env"


def environment_file():
    # Resolve on use, never read secrets or mutate the environment on import.
    return Path(os.environ.get("RAG_ENV_FILE") or _DEFAULT_ENV_FILE).resolve()


def environment_values():
    # override=False also gives the process priority during ${NAME} expansion.
    values = DotEnv(environment_file(), encoding="utf-8-sig", override=False).dict()
    return {**{key: value for key, value in values.items() if value is not None}, **os.environ}


class ProjectSettings(BaseSettings):
    model_config = SettingsConfigDict(
        extra="ignore", env_file_encoding="utf-8-sig", hide_input_in_errors=True,
    )

    def __init__(self, **values):
        # An explicit _env_file (including None) remains available for isolation.
        values.setdefault("_env_file", environment_file())
        super().__init__(**values)
