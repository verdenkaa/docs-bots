"""Генератор маленького PDF для тестов конвертера: собирается вручную, без зависимостей.

build_sample_pdf(tagged=True) даёт страницу с тегами структуры: заголовки, абзац с жирным
фрагментом, оглавление, вложенные списки, ссылка, таблица, рамка из одной ячейки, картинка
посреди предложения и колонтитул-артефакт. tagged=False — то же содержимое без тегов.

Посмотреть файл глазами:
    python tests/sample_pdf.py sample.pdf
"""

from __future__ import annotations

import sys
import zlib
from pathlib import Path

LINK_URI = "https://example.com/docs"
IMAGE_SIZE = (4, 4)

# (mcid, шрифт, [строки текста]) или (mcid, "image"); порядок = порядок на странице сверху вниз.
# SAME_LINE — mcid, которые продолжают строку предыдущего элемента, а не начинают новую.
SAME_LINE = {2, 3, 10, 12, 14, 17, 18}
_CONTENT = [
    (0, "F2", ["1 Getting started"]),
    (1, "F1", ["Plain text and "]),
    (2, "F2", ["bold text"]),
    (3, "F1", [" in one paragraph."]),
    (4, "F1", ["1 Getting started ......... 1"]),
    (5, "F2", ["1.1 Details"]),
    (6, "F1", ["1. First step"]),
    (20, "F1", ["Note inside list"]),  # рамка между пунктами; в тегах стоит после всего списка
    (7, "F1", ["2. Second step"]),
    (8, "F1", ["o nested item"]),
    (9, "F1", ["See "]),
    (10, "F1", ["the docs"]),
    (11, "F2", ["Name"]),
    (12, "F2", ["Value"]),
    (13, "F1", ["Budget"]),
    (14, "F1", ["Devia", "tion"]),  # слово разорвано переносом строки внутри ячейки
    (15, "F1", ["Important note"]),
    (16, "F1", ["Press "]),
    (17, "image"),
    (18, "F1", [" to save."]),
    (19, "F1", ["Type - kind", "Model - model of item"]),  # один абзац с явным переносом строки
]

# (тег, атрибуты, дети); ребёнок-число — это MCID, "OBJR" — ссылка на аннотацию
_STRUCTURE = ("Document", {}, [
    ("H1", {}, [0]),
    ("P", {}, [1, 2, 3]),
    ("TOC", {}, [("TOCI", {}, [4])]),
    ("H2", {}, [5]),
    ("L", {"ListNumbering": "Decimal"}, [
        ("LI", {}, [("LBody", {}, [6])]),
        ("LI", {}, [("LBody", {}, [7, ("L", {"ListNumbering": "Circle"}, [("LI", {}, [("LBody", {}, [8])])])])]),
    ]),
    ("Table", {}, [("TR", {}, [("TD", {}, [("P", {}, [20])])])]),
    ("P", {}, [9, ("Link", {}, [10, "OBJR"])]),
    ("Table", {}, [
        ("TR", {}, [("TH", {}, [11]), ("TH", {}, [12])]),
        ("TR", {}, [("TD", {}, [13]), ("TD", {}, [14])]),
    ]),
    ("Table", {}, [("TR", {}, [("TD", {}, [("P", {}, [15])])])]),
    # как в Word: картинка записана после текста абзаца, хотя стоит посреди строки
    ("P", {}, [16, 18, ("Figure", {"Alt": "inline/button.png"}, [17])]),
    ("P", {}, [19]),
])


def _content_stream() -> bytes:
    ops = ["/Artifact BMC BT /F1 9 Tf 50 810 Td (Page header) Tj ET EMC"]
    x, y = 50, 802
    for item in _CONTENT:
        mcid = item[0]
        if mcid in SAME_LINE:
            x += 150
        else:
            x, y = 50, y - 22
        if item[1] == "image":
            ops.append(f"/Figure <</MCID {mcid}>> BDC q 12 0 0 12 {x} {y - 2} cm /Im1 Do Q EMC")
            continue
        _, font, lines = item
        ops.append(f"/Span <</MCID {mcid}>> BDC")
        for number, line in enumerate(lines):
            if number:
                y -= 22
            ops.append(f"BT /{font} 11 Tf {x} {y} Td ({line}) Tj ET")
        ops.append("EMC")
    return "\n".join(ops).encode("ascii")


def build_sample_pdf(*, tagged: bool = True, untagged_text: str = "") -> bytes:
    """untagged_text — строка видимого текста, оставленная вне тегов (как в плохо размеченном PDF)."""
    objects: dict[int, bytes] = {}

    def add(number: int, body: str | bytes) -> None:
        objects[number] = body if isinstance(body, bytes) else body.encode("ascii")

    def stream(number: int, header: str, data: bytes) -> None:
        add(number, f"<<{header} /Length {len(data)}>>\nstream\n".encode("ascii") + data + b"\nendstream")

    catalog, pages, page, content, font, bold, image, link, struct_root = range(1, 10)
    next_free = [struct_root + 1]

    def struct_elem(spec, parent: int) -> int:
        tag, attrs, kids = spec
        number = next_free[0]
        next_free[0] += 1
        refs = []
        for kid in kids:
            if kid == "OBJR":
                refs.append(f"<</Type /OBJR /Obj {link} 0 R /Pg {page} 0 R>>")
            elif isinstance(kid, int):
                refs.append(str(kid))
            else:
                refs.append(f"{struct_elem(kid, number)} 0 R")
        extra = ""
        if "Alt" in attrs:
            extra += f" /Alt ({attrs['Alt']})"
        if "ListNumbering" in attrs:
            extra += f" /A <</O /List /ListNumbering /{attrs['ListNumbering']}>>"
        add(number, f"<</Type /StructElem /S /{tag} /P {parent} 0 R /Pg {page} 0 R /K [{' '.join(refs)}]{extra}>>")
        return number

    tags = ""
    if tagged:
        document = struct_elem(_STRUCTURE, struct_root)
        add(struct_root, f"<</Type /StructTreeRoot /K [{document} 0 R]>>")
        tags = f" /StructTreeRoot {struct_root} 0 R /MarkInfo <</Marked true>>"
    add(catalog, f"<</Type /Catalog /Pages {pages} 0 R{tags}>>")
    add(pages, f"<</Type /Pages /Kids [{page} 0 R] /Count 1>>")
    add(page, (
        f"<</Type /Page /Parent {pages} 0 R /MediaBox [0 0 595 842] /Contents {content} 0 R "
        f"/Resources <</Font <</F1 {font} 0 R /F2 {bold} 0 R>> /XObject <</Im1 {image} 0 R>>>> "
        f"/Annots [{link} 0 R]>>"
    ))
    extra = f"\nBT /F1 11 Tf 50 100 Td ({untagged_text}) Tj ET".encode("ascii") if untagged_text else b""
    stream(content, "", _content_stream() + extra)
    add(font, "<</Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding>>")
    add(bold, "<</Type /Font /Subtype /Type1 /BaseFont /Helvetica-Bold /Encoding /WinAnsiEncoding>>")
    width, height = IMAGE_SIZE
    stream(
        image,
        f"/Type /XObject /Subtype /Image /Width {width} /Height {height} /ColorSpace /DeviceRGB "
        "/BitsPerComponent 8 /Filter /FlateDecode",
        zlib.compress(bytes([200, 30, 30]) * (width * height)),
    )
    add(link, f"<</Type /Annot /Subtype /Link /Rect [50 570 120 590] /Border [0 0 0] /A <</S /URI /URI ({LINK_URI})>>>>")

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += f"{number} 0 obj\n".encode("ascii") + objects[number] + b"\nendobj\n"
    xref_at = len(out)
    size = max(objects) + 1
    out += f"xref\n0 {size}\n".encode("ascii") + b"0000000000 65535 f \n"
    for number in range(1, size):
        out += f"{offsets[number]:010d} 00000 n \n".encode("ascii") if number in offsets else b"0000000000 65535 f \n"
    out += f"trailer\n<</Size {size} /Root {catalog} 0 R>>\nstartxref\n{xref_at}\n%%EOF\n".encode("ascii")
    return bytes(out)


if __name__ == "__main__":
    target = Path(sys.argv[1] if len(sys.argv) > 1 else "sample.pdf")
    target.write_bytes(build_sample_pdf())
    print(target)
