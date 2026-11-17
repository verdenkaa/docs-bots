"""In-memory parsing for DOCX, PDF and UTF-8 Markdown.

CPU parsing runs in _convert_worker. Parsers do not call models, access S3,
download linked images or persist the original. PDF tags enrich the structure;
untagged text is accepted, image pages without a text layer are unsupported.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Protocol


AddImage = Callable[[bytes], str]


class ParseRefused(Exception):
    """Stable refusal code; reason contains no source text."""

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code, self.reason = code, reason


class DocumentParser(Protocol):
    def parse(self, data: bytes, add_image: AddImage) -> str: ...


class MarkdownParser:
    def parse(self, data: bytes, add_image: AddImage) -> str:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ParseRefused("conversion_failed", "Markdown is not UTF-8") from None


class PdfParser:
    def parse(self, data: bytes, add_image: AddImage) -> str:
        from rag.ingestion._tagged_pdf import TaggedPdfConverter

        return TaggedPdfConverter(data, add_image).convert()


class DocxParser:
    def parse(self, data: bytes, add_image: AddImage) -> str:
        from rag.ingestion._docx import DocxConverter

        return DocxConverter(data, add_image).convert()


PARSERS: dict[str, DocumentParser] = {"md": MarkdownParser(), "pdf": PdfParser(), "docx": DocxParser()}


def to_png(data: bytes) -> bytes:
    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as image:
            if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                image = image.convert("RGB")
            target = BytesIO()
            image.save(target, format="PNG")
    except (OSError, ValueError, Image.DecompressionBombError):
        raise ParseRefused("conversion_failed", "Document image cannot be decoded") from None
    return target.getvalue()
