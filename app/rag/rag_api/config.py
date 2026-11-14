"""Optional local dotenv; credentials are validated only when the API starts/uses them."""
import os
from pathlib import Path


ENV_PATH = Path(__file__).resolve().parents[2] / ".env"


def load_env() -> None:
    if not ENV_PATH.is_file():
        return

    for line in ENV_PATH.read_text(
        encoding="utf-8-sig"
    ).splitlines():
        line = line.strip()

        if not line or line.startswith("#") or "=" not in line:
            continue

        key, value = line.split("=", 1)
        os.environ.setdefault(
            key.strip(),
            value.strip().strip('"').strip("'"),
        )


WEBHOOK_PATH = "/telegram/webhook"

def _get_values():
    load_env()

    BOT_TOKEN = os.environ.get("BOT_TOKEN", "")
    WEBHOOK_SECRET = os.environ.get("TELEGRAM_WEBHOOK_SECRET", "")
    WEBHOOK_BASE_URL = os.environ.get("TELEGRAM_WEBHOOK_URL", "").rstrip("/")
    TEST_MODE = os.getenv("TEST_MODE", "0") == "1"

    TELEGRAM_API_URL = f"https://api.telegram.org/bot{BOT_TOKEN}"

    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN не задан")

    if not WEBHOOK_SECRET:
        raise ValueError("TELEGRAM_WEBHOOK_SECRET не задан")

    if not WEBHOOK_BASE_URL.startswith("https://"):
        raise ValueError("TELEGRAM_WEBHOOK_URL должен быть HTTPS")

    if not (
        1 <= len(WEBHOOK_SECRET) <= 256
        and all(
            c.isascii() and (c.isalnum() or c in "_-")
            for c in WEBHOOK_SECRET
        )
    ):
        raise ValueError("Некорректный TELEGRAM_WEBHOOK_SECRET")
    return locals()

def __getattr__(name):
    if name in {"BOT_TOKEN", "WEBHOOK_SECRET", "WEBHOOK_BASE_URL", "TEST_MODE", "TELEGRAM_API_URL"}:
        return _get_values()[name]
    raise AttributeError(name)
