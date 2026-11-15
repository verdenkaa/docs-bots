"""Deterministic Markdown splitting; image placeholders and code/tables stay whole."""

import asyncio
import os
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from rag.contracts import SourceLocator, StepError, StorageError
from rag.ingestion.contracts import ConvertedDocument, PreparedChunk, StagedUpload


_IMAGE = r"!\[(?:\\.|[^\]\\])*\]\(image://([^)]+)\)"
_TOKEN = re.compile(_IMAGE + r"|\s+|(?:(?!!\[)\S)+|.", re.DOTALL)


class _ChunkSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    characters: int = Field(default=2000, gt=0)
    overlap: int = Field(default=200, ge=0)
    max_markdown_bytes: int = Field(default=20 * 1024 * 1024, gt=0)

    @model_validator(mode="after")
    def _limits(self):
        if self.overlap >= self.characters:
            raise ValueError("Overlap must be smaller than the chunk budget")
        return self

    @classmethod
    def from_environment(cls):
        try:
            values = {key: os.environ[env] for key, env in (
                ("characters", "INGESTION_CHUNK_CHARACTERS"),
                ("overlap", "INGESTION_CHUNK_OVERLAP"),
                ("max_markdown_bytes", "RAG_MAX_MARKDOWN_BYTES"),
            ) if env in os.environ}
            result = cls.model_validate(values)
            embedding = os.environ.get("INGESTION_EMBEDDING_INPUT_CHARACTERS")
            if embedding is not None and result.characters > int(embedding):
                raise ValueError("Chunk budget exceeds the embedding input budget")
            return result
        except (ValidationError, ValueError):
            raise StepError("ingestion_configuration_error", False) from None


def _read(upload, maximum):
    try:
        if upload.path.is_symlink() or upload.path.stat().st_size > maximum:
            raise StorageError("Unsafe or oversized staged Markdown")
        with upload.path.open("rb") as stream:
            content = stream.read(maximum + 1)
        if len(content) > maximum:
            raise StorageError("Oversized staged Markdown")
        text = content.decode("utf-8")
        ConvertedDocument(markdown=text, images=upload.images)
        return text
    except (OSError, UnicodeError, ValidationError):
        raise StorageError("Staged Markdown is unavailable or invalid") from None


def _blocks(text):
    """Yield section changes and blocks; only plain paragraphs may be split."""
    lines = text.splitlines(keepends=True)
    position = 0
    while position < len(lines):
        line = lines[position]
        heading = re.match(r"^ {0,3}#{1,6}\s+(.+?)\s*#*\s*$", line)
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if heading:
            yield line, heading[1], True
            position += 1
            continue
        block = [line]
        position += 1
        indivisible = bool(fence) or line.lstrip().startswith("|")
        if fence:
            closing = re.compile(r"^ {0,3}" + re.escape(fence[1][0]) + "{" + str(len(fence[1])) + r",}\s*$")
            while position < len(lines):
                current = lines[position]
                block.append(current)
                position += 1
                if closing.match(current):
                    break
        else:
            while position < len(lines):
                current = lines[position]
                if (not current.strip() or re.match(r"^ {0,3}(?:#{1,6}\s|`{3,}|~{3,})", current)
                        or current.lstrip().startswith("|") != line.lstrip().startswith("|")):
                    break
                block.append(current)
                position += 1
        yield "".join(block), None, indivisible


def _suffix(text, maximum):
    selected, size = [], 0
    for match in reversed(list(_TOKEN.finditer(text))):
        token = match[0]
        if size + len(token) > maximum:
            break
        selected.append(token)
        size += len(token)
    return "".join(reversed(selected))


def _split(text, upload, settings):
    from uuid import UUID

    images = {image.image_id: image for image in upload.images}
    result, current, section = [], "", None

    def flush():
        nonlocal current
        if current.strip():
            refs = list(dict.fromkeys(UUID(match[1]) for match in re.finditer(_IMAGE, current)))
            result.append(PreparedChunk(text=current.strip(),
                locator=SourceLocator(section=section) if section else None,
                images=[images[identifier] for identifier in refs]))
        current = ""

    for block, heading, indivisible in _blocks(text):
        if heading is not None:
            flush()
            section = heading
        parts = [block] if indivisible else [match[0] for match in _TOKEN.finditer(block)]
        for part in parts:
            if len(part) > settings.characters:
                raise StepError("chunk_too_large", False)
            if len(current) + len(part) > settings.characters:
                overlap = _suffix(current, min(settings.overlap, settings.characters - len(part)))
                flush()
                current = overlap
            current += part
    flush()
    return result


async def _prepare_chunks(upload: StagedUpload) -> list[PreparedChunk]:
    upload = StagedUpload.model_validate_json(upload.model_dump_json())
    settings = _ChunkSettings.from_environment()
    text = await asyncio.to_thread(_read, upload, settings.max_markdown_bytes)
    return await asyncio.to_thread(_split, text, upload, settings)
