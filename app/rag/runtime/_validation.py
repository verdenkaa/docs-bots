"""Shared checks on the infrastructure boundaries."""

import math
import re
from datetime import datetime
from uuid import UUID

from rag.contracts import ImageContent, StepError


def _positive(value):
    if type(value) is not int or value <= 0:
        raise ValueError("Expected a positive integer")
    return value


def _utc(value):
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Expected a timezone-aware datetime")
    return value


def _text(value):
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Expected nonempty text")
    return value


def _staging_revision(value):
    if not isinstance(value, str) or not re.fullmatch(r"uploads/[0-9a-f-]{36}", value):
        raise ValueError("Invalid staging reference")
    revision = UUID(value.split("/")[1])
    if value != f"uploads/{revision}":
        raise ValueError("Noncanonical staging reference")
    return revision


def _vector(value, dimensions):
    if len(value) != dimensions or any(type(x) not in (int, float) or not math.isfinite(x) for x in value):
        raise StepError("embedding_profile_mismatch", False)
    return list(value)


def _image(data, declared=None):
    # Check the file signature, never the extension or provider Content-Type alone.
    if data.startswith(b"\x89PNG\r\n\x1a\n") and len(data) >= 24 and data[12:16] == b"IHDR":
        mime = "image/png"
    elif data.startswith(b"\xff\xd8\xff") and len(data) >= 4:
        mime = "image/jpeg"
    elif data[:6] in (b"GIF87a", b"GIF89a") and len(data) >= 13:
        mime = "image/gif"
    elif data[:4] == b"RIFF" and data[8:12] == b"WEBP" and len(data) >= 16:
        mime = "image/webp"
    else:
        raise StepError("invalid_image", False)
    if declared is not None and declared.lower().split(";", 1)[0] != mime:
        raise StepError("image_mime_mismatch", False)
    return ImageContent(data=data, mime_type=mime)
