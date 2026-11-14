"""Разбор документов в Markdown: один интерфейс, по реализации на формат.

Внутренняя часть Document Converter (docs/documents/ingestion/converter.md). Парсеры синхронные
и работают только с памятью: оригинал на диск не пишется, сеть и файлы по ссылкам из документа
не читаются. PDF и DOCX разбираются в управляемом процессе (_convert_worker), Markdown — на месте.

Картинка отдаётся в add_image как PNG; строка, которую вернул add_image, остаётся в Markdown
на месте картинки. Описание и image_id назначает convert_document.

Библиотеки разбора импортируются при вызове парсера, не при импорте модуля.
"""

from __future__ import annotations

import base64
import binascii
import re
import unicodedata
from collections.abc import Callable
from io import BytesIO
from typing import Protocol

AddImage = Callable[[bytes], str]

TEXT_CHECK_PAGES = 5  # сколько первых страниц смотреть при проверке текстового слоя
MAX_BROKEN_CHARS = 0.1  # допустимая доля нечитаемых символов
DATA_URI_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(data:image/[\w.+-]+;base64,([A-Za-z0-9+/=\s]+)\)")


class ParseRefused(Exception):
    """Окончательный отказ разбора: повтор с тем же файлом даст тот же итог.

    code — код StepError контракта convert_document; reason — пояснение для диагностики
    без текста документа.
    """

    def __init__(self, code: str, reason: str) -> None:
        super().__init__(reason)
        self.code = code
        self.reason = reason


class DocumentParser(Protocol):
    """Интерфейс обработчика одного формата."""

    def parse(self, data: bytes, add_image: AddImage) -> str:
        """Вернуть Markdown документа; каждую картинку передать в add_image и вставить его результат."""
        ...


class MarkdownParser:
    """Готовый Markdown пропускается как есть.

    Картинки по ссылкам и путям не загружаются: ссылки остаются обычным текстом Markdown.
    """

    def parse(self, data: bytes, add_image: AddImage) -> str:
        try:
            return data.decode("utf-8-sig")
        except UnicodeDecodeError:
            raise ParseRefused("conversion_failed", "Markdown не в UTF-8") from None


class PdfParser:
    """PDF с тегами структуры читается по тегам; без тегов или с формальными тегами — через docling."""

    def parse(self, data: bytes, add_image: AddImage) -> str:
        from rag.ingestion._tagged_pdf import TaggedPdfConverter, UntaggedPdfError

        _check_pdf_text(data)
        try:
            return TaggedPdfConverter(data, add_image).convert()
        except UntaggedPdfError:
            # тегов нет или на них нельзя полагаться — структуру распознаём по виду страницы
            return _convert_with_docling(data, "pdf", add_image)


class DocxParser:
    """DOCX читается по собственной разметке Word: стили, нумерация, таблицы, ссылки и картинки."""

    def parse(self, data: bytes, add_image: AddImage) -> str:
        from rag.ingestion._docx import DocxConverter

        return DocxConverter(data, add_image).convert()


PARSERS: dict[str, DocumentParser] = {"md": MarkdownParser(), "pdf": PdfParser(), "docx": DocxParser()}


def _check_pdf_text(data: bytes) -> None:
    """Отсекает PDF, из которых без OCR получится пустота или абракадабра."""
    import pdfplumber

    with pdfplumber.open(BytesIO(data)) as pdf:
        texts = [c["text"] for page in pdf.pages[:TEXT_CHECK_PAGES] for c in page.chars if c["text"].strip()]
    if not texts:
        raise ParseRefused("unsupported_format", "в PDF нет текстового слоя (скан или одни картинки), нужен OCR")
    # символы без таблицы соответствия Юникоду pdfminer отдаёт как "(cid:N)"
    broken = sum(
        t.startswith("(cid:") or t == "�" or unicodedata.category(t[0]) in ("Co", "Cn", "Cc") for t in texts
    )
    if broken / len(texts) > MAX_BROKEN_CHARS:
        raise ParseRefused(
            "unsupported_format",
            f"текстовый слой PDF не читается: {broken / len(texts):.0%} символов без соответствия Юникоду, нужен OCR",
        )


def _convert_with_docling(data: bytes, suffix: str, add_image: AddImage) -> str:
    try:
        from docling.datamodel.base_models import DocumentStream, InputFormat
        from docling.datamodel.pipeline_options import PdfPipelineOptions
        from docling.document_converter import DocumentConverter, PdfFormatOption
        from docling_core.types.doc import ImageRefMode
    except ImportError:
        # docling — необязательная зависимость профиля; без неё обработчика для такого файла нет
        raise ParseRefused("unsupported_format", f"для этого {suffix} нужен docling, он не установлен") from None

    options = PdfPipelineOptions()
    options.do_ocr = False
    options.do_table_structure = True
    options.generate_picture_images = True
    options.images_scale = 2.0
    converter = DocumentConverter(format_options={InputFormat.PDF: PdfFormatOption(pipeline_options=options)})
    document = converter.convert(DocumentStream(name=f"document.{suffix}", stream=BytesIO(data))).document
    # картинки встраиваются в текст, чтобы каждая осталась ровно на своём месте
    markdown = document.export_to_markdown(image_mode=ImageRefMode.EMBEDDED)
    markdown = DATA_URI_IMAGE_RE.sub(lambda match: add_image(_embedded_png(match.group(1))), markdown)
    return markdown.replace("<!-- image -->", "")


def _embedded_png(encoded: str) -> bytes:
    try:
        return to_png(base64.b64decode(encoded))
    except binascii.Error:
        raise ParseRefused("conversion_failed", "картинка документа не декодируется") from None


def to_png(data: bytes) -> bytes:
    """Перекодировать картинку документа в PNG: дальше конвертер работает только с этим форматом."""
    from PIL import Image

    try:
        with Image.open(BytesIO(data)) as image:
            if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                image = image.convert("RGB")
            target = BytesIO()
            image.save(target, format="PNG")
    except (OSError, ValueError):
        raise ParseRefused("conversion_failed", "картинка документа не декодируется") from None
    return target.getvalue()
