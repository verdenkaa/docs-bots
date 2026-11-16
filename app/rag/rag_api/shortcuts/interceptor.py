"""Contracts: docs/documents/telegram/module/shortcuts.md.

Pure exact matching; no storage, queues, model calls or Telegram requests.
"""

import re
import unicodedata
from typing import Literal

from pydantic import model_validator

from rag.dto import DTO, NonEmptyText
from . import registry


class ShortcutResult(DTO):
    intent: NonEmptyText
    action: Literal["reply", "operator"]
    response: NonEmptyText | None = None

    @model_validator(mode="after")
    def check_response(self) -> "ShortcutResult":
        if (self.action == "reply") != (self.response is not None):
            raise ValueError("Only reply requires response")
        return self


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = text.casefold().replace("ё", "е")
    return " ".join(re.sub(r"[\W_]+", " ", text, flags=re.UNICODE).split())


def _build_shortcuts() -> dict[str, ShortcutResult]:
    index: dict[str, ShortcutResult] = {}
    reply_intents = set(registry.REPLIES) | registry.ISSUE_INTENTS
    overlap = reply_intents & registry.OPERATOR_INTENTS
    if overlap:
        raise ValueError(f"Intent has conflicting actions: {sorted(overlap)}")
    missing = set(registry.PHRASES) - (reply_intents | registry.OPERATOR_INTENTS)
    if missing:
        raise ValueError(f"Unknown intents: {sorted(missing)}")
    for intent, raw_phrases in registry.PHRASES.items():
        shortcut = ShortcutResult(
            intent=intent,
            action="operator" if intent in registry.OPERATOR_INTENTS else "reply",
            response=(None if intent in registry.OPERATOR_INTENTS else
                      registry.ISSUE_REPLY if intent in registry.ISSUE_INTENTS else
                      registry.REPLIES[intent]),
        )
        for phrase in re.split(r"[\n|]", raw_phrases):
            if not phrase.strip():
                continue
            key = normalize(phrase)
            if not key:
                raise ValueError(f"Empty normalized phrase: {phrase!r}")
            existing = index.get(key)
            if existing is not None and existing.intent != intent:
                raise ValueError(f"Shortcut collision: {phrase!r}; intents={existing.intent!r}, {intent!r}")
            index[key] = shortcut
    return index


_shortcuts = _build_shortcuts()


def intercept_message(text: str | None) -> ShortcutResult | None:
    """Return reply/operator on an exact normalized match, otherwise None (miss)."""
    if not text or not text.strip():
        return None
    result = _shortcuts.get(normalize(text))
    return result.model_copy() if result is not None else None
