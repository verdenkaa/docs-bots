"""Deterministic Markdown splitting; image placeholders and code/tables stay whole."""

import asyncio
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from rag._configuration import environment_values
from rag.contracts import SourceLocator, StepError, StorageError
from rag.ingestion.contracts import ConvertedDocument, PreparedChunk, StagedUpload


_IMAGE = r"!\[(?:\\.|[^\]\\])*\]\(image://([^)]+)\)"
_TOKEN = re.compile(_IMAGE + r"|\s+|(?:(?!!\[)\S)+|.", re.DOTALL)
_SENTENCE_END = re.compile(r'''[.!?…]["'»”\)\]]*$''')


class _Block:
    """Private parsing state, not a boundary DTO."""

    def __init__(self, text: str, protected: bool = False,
                 heading: str | None = None, level: int = 0):
        self.text, self.protected = text, protected
        self.heading, self.level = heading, level


class _Section:
    """A local Markdown tree; never stored in DocumentBatch."""

    def __init__(self, heading: str | None = None, level: int = 0, blocks=None):
        self.heading, self.level = heading, level
        self.blocks = [] if blocks is None else blocks
        self.children = []

    def contents(self):
        yield from self.blocks
        for child in self.children:
            yield from child.contents()


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
            environment = environment_values()
            values = {key: environment[env] for key, env in (
                ("characters", "INGESTION_CHUNK_CHARACTERS"),
                ("overlap", "INGESTION_CHUNK_OVERLAP"),
                ("max_markdown_bytes", "RAG_MAX_MARKDOWN_BYTES"),
            ) if env in environment}
            result = cls.model_validate(values)
            embedding = environment.get("INGESTION_EMBEDDING_INPUT_CHARACTERS")
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
    """Keep Markdown boundaries until packing and overlap have both finished."""
    lines = text.splitlines(keepends=True)
    position = 0
    while position < len(lines):
        line = lines[position]
        heading = re.match(r"^ {0,3}(#{1,6})[ \t]+(.+?)\s*#*\s*$", line)
        fence = re.match(r"^ {0,3}(`{3,}|~{3,})", line)
        if heading:
            yield _Block(line, True, heading[2], len(heading[1]))
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
        yield _Block("".join(block), indivisible)


def _sections(blocks):
    root = _Section()
    stack = [root]
    for block in blocks:
        if block.heading is not None:
            while stack[-1].level >= block.level:
                stack.pop()
            section = _Section(block.heading, block.level, [block])
            stack[-1].children.append(section)
            stack.append(section)
        else:
            stack[-1].blocks.append(block)
    return root


def _sentences(text):
    """A punctuation boundary outside an image placeholder is a fallback split."""
    current = []
    ended = False
    for match in _TOKEN.finditer(text):
        token = match[0]
        current.append(token)
        if token.isspace():
            if ended:
                yield "".join(current)
                current = []
            ended = False
        else:
            ended = bool(_SENTENCE_END.search(token))
    if current:
        yield "".join(current)


def _parts(block, maximum):
    if len(block.text.strip()) <= maximum:
        yield block
    elif block.protected:
        raise StepError("chunk_too_large", False)
    else:
        for sentence in _sentences(block.text):
            if len(sentence.strip()) <= maximum:
                yield _Block(sentence)
                continue
            for match in _TOKEN.finditer(sentence):
                token = match[0]
                if len(token.strip()) > maximum:
                    raise StepError("chunk_too_large", False)
                yield _Block(token)


def _suffix(blocks, maximum):
    """Take a contiguous suffix; never enter a protected block partway."""
    selected, size = [], 0
    for block in reversed(blocks):
        parts = [block] if block.protected else [
            _Block(match[0]) for match in _TOKEN.finditer(block.text)
        ]
        for part in reversed(parts):
            if size + len(part.text) > maximum:
                return list(reversed(selected))
            selected.append(part)
            size += len(part.text)
    return list(reversed(selected))


def _split(text, upload, settings):
    from uuid import UUID

    images = {image.image_id: image for image in upload.images}
    result = []

    def emit(blocks, section):
        text = "".join(block.text for block in blocks).strip()
        if text:
            refs = list(dict.fromkeys(UUID(match[1]) for match in re.finditer(_IMAGE, text)))
            result.append(PreparedChunk(text=text,
                locator=SourceLocator(section=section) if section else None,
                images=[images[identifier] for identifier in refs]))

    def pack(blocks, section):
        current = []
        current_text = ""
        for block in blocks:
            for part in _parts(block, settings.characters):
                if len((current_text + part.text).strip()) > settings.characters:
                    emit(current, section)
                    maximum = min(settings.overlap,
                                  settings.characters - len(part.text.rstrip()))
                    current = _suffix(current, max(0, maximum))
                    current_text = "".join(item.text for item in current)
                current.append(part)
                current_text += part.text
        emit(current, section)

    def split_section(section, prefix=()):
        blocks = [*prefix, *section.blocks]
        contents = [*prefix, *section.contents()]
        # The document root separates its major sections even when all fit.
        if not section.children or (section.level and len(
                "".join(block.text for block in contents).strip()) <= settings.characters):
            pack(contents, section.heading)
            return
        # Carry a parent heading into its first subsection instead of producing
        # a chunk containing only that heading. An introduction has its own text.
        if any(block.text.strip() and block.heading is None for block in blocks):
            pack(blocks, section.heading)
            blocks = []
        for child in section.children:
            split_section(child, blocks)
            blocks = []

    split_section(_sections(_blocks(text)))
    return result


async def _prepare_chunks(upload: StagedUpload) -> list[PreparedChunk]:
    upload = StagedUpload.model_validate_json(upload.model_dump_json())
    settings = _ChunkSettings.from_environment()
    text = await asyncio.to_thread(_read, upload, settings.max_markdown_bytes)
    return await asyncio.to_thread(_split, text, upload, settings)
