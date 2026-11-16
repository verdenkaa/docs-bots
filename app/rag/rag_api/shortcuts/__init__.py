"""Local classification of typical Telegram text messages."""

from .interceptor import ShortcutResult, intercept_message

__all__ = ["ShortcutResult", "intercept_message"]
