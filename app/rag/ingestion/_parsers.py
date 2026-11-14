"""In-memory parsing for DOCX, tagged PDF and UTF-8 Markdown.

CPU parsing runs in _convert_worker. Parsers do not call models, access S3,
download linked images or persist the original. Untagged/scanned PDF is outside
this profile; there is deliberately no Docling fallback.
"""

from __future__ import annotations

from collections.abc import Callable
from io import BytesIO
from typing import Protocol
import unicodedata


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
        from rag.ingestion._tagged_pdf import TaggedPdfConverter, UntaggedPdfError

        _check_pdf_text(data)
        try:
            return TaggedPdfConverter(data, add_image).convert()
        except UntaggedPdfError:
            raise ParseRefused("unsupported_format", "PDF requires complete structural tags") from None


class DocxParser:
    def parse(self, data: bytes, add_image: AddImage) -> str:
        from rag.ingestion._docx import DocxConverter

        return DocxConverter(data, add_image).convert()


PARSERS: dict[str, DocumentParser] = {"md": MarkdownParser(), "pdf": PdfParser(), "docx": DocxParser()}


def _check_pdf_text(data: bytes) -> None:
    import pdfplumber

    with pdfplumber.open(BytesIO(data)) as pdf:
        texts = [char["text"] for page in pdf.pages[:5] for char in page.chars if char["text"].strip()]
    if not texts:
        raise ParseRefused("unsupported_format", "PDF has no text layer")
    broken = sum(text.startswith("(cid:") or text == "�"
                 or unicodedata.category(text[0]) in ("Co", "Cn", "Cc") for text in texts)
    if broken / len(texts) > 0.1:
        raise ParseRefused("unsupported_format", "PDF text layer is unreadable")


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