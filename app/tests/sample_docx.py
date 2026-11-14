"""Генератор маленького DOCX для тестов конвертера: собирается вручную из XML, без зависимостей.

В документе: название, оглавление, заголовки, абзац с жирным фрагментом и удалённым в режиме правки
текстом, нумерованный список с вложенным маркированным и абзацем внутри пункта, ссылка, таблица
с объединённой ячейкой, рамка из одной ячейки, картинка посреди предложения и явный перенос строки.
"""

from __future__ import annotations

import io
import struct
import zipfile
import zlib

LINK_URI = "https://example.com/docs"
IMAGE_SIZE = (4, 4)

_NS = ('xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
       'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships" '
       'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"')
_RELS_NS = 'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"'
_REL_TYPE = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"


def _p(content: str, style: str = "", num: tuple[int, int] | None = None) -> str:
    properties = f'<w:pStyle w:val="{style}"/>' if style else ""
    if num:
        properties += f'<w:numPr><w:ilvl w:val="{num[1]}"/><w:numId w:val="{num[0]}"/></w:numPr>'
    return f"<w:p><w:pPr>{properties}</w:pPr>{content}</w:p>"


def _r(text: str, bold: bool = False) -> str:
    properties = "<w:rPr><w:b/></w:rPr>" if bold else ""
    return f'<w:r>{properties}<w:t xml:space="preserve">{text}</w:t></w:r>'


def _cell(content: str, span: int = 1) -> str:
    return f'<w:tc><w:tcPr><w:gridSpan w:val="{span}"/></w:tcPr>{content}</w:tc>'


_IMAGE = '<w:r><w:drawing><a:graphic><a:graphicData><a:blip r:embed="rId2"/></a:graphicData></a:graphic></w:drawing></w:r>'

_BODY = "".join([
    _p(_r("Sample document"), "Title"),
    '<w:sdt><w:sdtPr><w:docPartObj><w:docPartGallery w:val="Table of Contents"/></w:docPartObj></w:sdtPr>'
    f'<w:sdtContent>{_p(_r("1 Getting started ......... 1"), "TOC1")}</w:sdtContent></w:sdt>',
    _p(_r("Getting started"), "Heading1"),
    _p(_r("Plain text and ") + _r("bold ", True) + _r("text", True) + "<w:del><w:r><w:delText>removed</w:delText></w:r></w:del>"
       + _r(" in one paragraph.")),
    _p(_r("Details"), "Heading2"),
    _p(_r("First step"), "ListParagraph", (1, 0)),
    _p(_r("Note inside list")),
    _p(_r("nested item"), "ListParagraph", (2, 1)),
    _p(_r("Second step"), "ListParagraph", (1, 0)),
    _p(_r("See ") + f'<w:hyperlink r:id="rId1">{_r("the docs")}</w:hyperlink>'),
    "<w:tbl>"
    f"<w:tr>{_cell(_p(_r('Name', True)))}{_cell(_p(_r('Value', True)))}</w:tr>"
    f"<w:tr>{_cell(_p(_r('Filter')))}{_cell(_p(_r('!&gt; 200 | x')))}</w:tr>"
    f"<w:tr>{_cell(_p(_r('Merged')), span=2)}</w:tr>"
    "</w:tbl>",
    f"<w:tbl><w:tr>{_cell(_p(_r('Important note')))}</w:tr></w:tbl>",
    _p(_r("Press ") + _IMAGE + _r(" to save.")),
    _p(_r("Type - kind") + "<w:r><w:br/></w:r>" + _r("Model - model of item")),
    "<w:sectPr/>",
])

_STYLES = "".join(
    f'<w:style w:type="paragraph" w:styleId="{style_id}"><w:name w:val="{name}"/>{extra}</w:style>'
    for style_id, name, extra in [
        ("Title", "Title", ""),
        ("Heading1", "heading 1", "<w:rPr><w:b/></w:rPr>"),
        ("Heading2", "heading 2", '<w:basedOn w:val="Heading1"/>'),
        ("TOC1", "toc 1", ""),
        ("ListParagraph", "List Paragraph", ""),
    ]
)

_NUMBERING = (
    '<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/></w:lvl></w:abstractNum>'
    '<w:abstractNum w:abstractNumId="1"><w:lvl w:ilvl="1"><w:numFmt w:val="bullet"/></w:lvl></w:abstractNum>'
    '<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num><w:num w:numId="2"><w:abstractNumId w:val="1"/></w:num>'
)

_DOCUMENT_RELS = (
    f'<Relationship Id="rId1" Type="{_REL_TYPE}/hyperlink" Target="{LINK_URI}" TargetMode="External"/>'
    f'<Relationship Id="rId2" Type="{_REL_TYPE}/image" Target="media/image1.png"/>'
)


def _png() -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    width, height = IMAGE_SIZE
    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def build_sample_docx(document_prefix: str = "") -> bytes:
    """Собрать DOCX; document_prefix вставляется перед корнем document.xml (для проверки отказа на DTD)."""
    head = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    parts = {
        "[Content_Types].xml": '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>',
        "word/document.xml": f"{document_prefix}<w:document {_NS}><w:body>{_BODY}</w:body></w:document>",
        "word/styles.xml": f"<w:styles {_NS}>{_STYLES}</w:styles>",
        "word/numbering.xml": f"<w:numbering {_NS}>{_NUMBERING}</w:numbering>",
        "word/_rels/document.xml.rels": f"<Relationships {_RELS_NS}>{_DOCUMENT_RELS}</Relationships>",
    }
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, text in parts.items():
            archive.writestr(name, (head + text).encode("utf-8"))
        archive.writestr("word/media/image1.png", _png())
    return target.getvalue()
