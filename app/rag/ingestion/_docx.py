"""DOCX -> Markdown: внутренняя часть Document Converter.

Читает разметку Word напрямую (ZIP + XML стандартной библиотекой), без вызовов моделей:
заголовки и списки берутся из стилей и нумерации, таблицы, ссылки и картинки — из самого документа.
Колонтитулы включаются один раз, сноски — в скобках у ссылки на них.
Оглавление и удалённый в режиме правки текст в Markdown не попадают.
Внешние картинки по ссылкам не скачиваются.
"""

from __future__ import annotations

import posixpath
import re
import zipfile
from contextlib import contextmanager
from io import BytesIO
from xml.etree import ElementTree

from rag.ingestion._parsers import AddImage, ParseRefused, to_png

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"
A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
V = "{urn:schemas-microsoft-com:vml}"
MC = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
REL = "{http://schemas.openxmlformats.org/package/2006/relationships}"

DOCUMENT = "word/document.xml"
MAX_PART_BYTES = 256 * 1024 * 1024  # распакованный размер одной части архива: защита от zip-бомбы
HARD_BREAK = "  \n"  # два пробела в конце строки
INDENT = "    "
HEADING_RE = re.compile(r"(?:heading|заголовок)\s*([1-9])")
LIST_STYLE_RE = re.compile(r"list (bullet|number)\s*(\d)?")
ORDERED_RE = re.compile(r"^(\*\*)?(\d+)[.)]\s+")
BULLET_RE = re.compile(r"^[•▪■□◦·–]\s+")
URL_RE = re.compile(r"(?i)^(https?://|mailto:)\S+$")
UNORDERED_FORMATS = {"bullet", "none"}
VECTOR_SUFFIXES = (".emf", ".wmf")
SKIPPED = {f"{W}del", f"{W}moveFrom", f"{W}instrText", f"{W}delText", f"{W}pPr", f"{W}rPr"}
GRAPHICS = {f"{W}drawing", f"{W}pict", f"{W}object"}


class DocxConverter:
    def __init__(self, data: bytes, add_image: AddImage) -> None:
        self._add_image = add_image
        self._zip = zipfile.ZipFile(BytesIO(data))
        self._names = set(self._zip.namelist())
        self._part_name = DOCUMENT
        self._targets = self._relationships(DOCUMENT)
        self._styles = self._read_styles()
        self._numbering = self._read_numbering()
        self._counters: dict[tuple[str, int], int] = {}
        self._notes: dict[str, dict[str, ElementTree.Element]] = {}
        self._active_notes: set[tuple[str, str]] = set()
        self._metafiles: dict[str, bytes] = {}

    def convert(self) -> str:
        body = self._xml(DOCUMENT).find(f"{W}body")
        if body is None:
            raise ParseRefused("conversion_failed", "в DOCX нет тела документа")
        blocks: list[str] = []
        seen: set[str] = set()
        pending: list[ElementTree.Element] = []
        for node in self._block_nodes(body):
            pending.append(node)
            section = node.find(f"{W}pPr/{W}sectPr") if node.tag == f"{W}p" else None
            if section is not None:
                blocks.extend(self._section(pending, section, seen))
                pending = []
        blocks.extend(self._section(pending, body.find(f"{W}sectPr"), seen))
        return "\n\n".join(blocks) + "\n"

    def _section(self, nodes: list[ElementTree.Element], section: ElementTree.Element | None,
                 seen: set[str]) -> list[str]:
        container = ElementTree.Element("section")
        container.extend(nodes)
        return (self._margins(section, "header", seen) + self._blocks(container)
                + self._margins(section, "footer", seen))

    def _margins(self, section: ElementTree.Element | None, kind: str, seen: set[str]) -> list[str]:
        """Все явно заданные варианты колонтитула секции, каждый файл — один раз."""
        blocks: list[str] = []
        if section is not None:
            for reference in section.findall(f"{W}{kind}Reference"):
                name = self._related_part(reference.get(f"{R}id", ""))
                if name not in seen:
                    seen.add(name)
                    with self._in_part(name):
                        blocks.extend(self._blocks(self._xml(name)))
        return blocks

    @contextmanager
    def _in_part(self, name: str):
        """Ссылки и картинки разрешаются относительно собственной части, не document.xml."""
        previous = self._part_name, self._targets, self._counters
        self._part_name, self._targets, self._counters = name, self._relationships(name), {}
        try:
            yield
        finally:
            self._part_name, self._targets, self._counters = previous

    def _note(self, kind: str, note_id: str) -> str:
        name = f"word/{kind}s.xml"
        if kind not in self._notes:
            if name not in self._names:
                raise ParseRefused("conversion_failed", "DOCX note part is missing")
            self._notes[kind] = {
                note.get(f"{W}id", ""): note for note in self._xml(name).findall(f"{W}{kind}")
                if note.get(f"{W}type", "normal") == "normal"
            }
        note = self._notes[kind].get(note_id)
        key = kind, note_id
        if note is None or key in self._active_notes:
            raise ParseRefused("conversion_failed", "DOCX note reference is invalid")
        self._active_notes.add(key)
        try:
            with self._in_part(name):
                blocks = self._blocks(note)
            text = "; ".join(" ".join(block.split()) for block in blocks)
            return f" ({text})" if text else ""
        finally:
            self._active_notes.remove(key)

    # --- части архива -------------------------------------------------------------------------

    def _part(self, name: str) -> bytes:
        if self._zip.getinfo(name).file_size > MAX_PART_BYTES:
            raise ParseRefused("conversion_failed", "часть DOCX больше лимита распаковки")
        return self._zip.read(name)

    def _xml(self, name: str) -> ElementTree.Element:
        data = self._part(name)
        if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
            # в OOXML нет DTD; сущности — способ раздуть разбор
            raise ParseRefused("conversion_failed", "DOCX содержит DTD")
        return ElementTree.fromstring(data)

    def _relationships(self, part: str) -> dict[str, tuple[str, bool]]:
        """rId -> (цель, внешняя ли она) для указанной части документа."""
        name = posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")
        if name not in self._names:
            return {}
        return {
            rel.get("Id", ""): (rel.get("Target", ""), rel.get("TargetMode") == "External")
            for rel in self._xml(name).iter(f"{REL}Relationship")
        }

    def _related_part(self, relationship: str) -> str:
        target, external = self._targets.get(relationship, ("", True))
        if not target:
            raise ParseRefused("conversion_failed", "DOCX relationship is missing")
        if external:
            raise ParseRefused("unsupported_format", "External DOCX parts are not fetched")
        name = (target.lstrip("/") if target.startswith("/") else
                posixpath.normpath(posixpath.join(posixpath.dirname(self._part_name), target)))
        if name not in self._names:
            raise ParseRefused("conversion_failed", "DOCX related part is missing")
        return name

    def _read_styles(self) -> dict[str, dict]:
        """styleId -> свойства с учётом наследования: уровень заголовка, оглавление, список, жирность."""
        if "word/styles.xml" not in self._names:
            return {}
        raw: dict[str, dict] = {}
        for style in self._xml("word/styles.xml").iter(f"{W}style"):
            name = (self._val(style.find(f"{W}name")) or "").lower()
            ppr, rpr = style.find(f"{W}pPr"), style.find(f"{W}rPr")
            heading = HEADING_RE.fullmatch(name)
            listed = LIST_STYLE_RE.fullmatch(name)
            raw[style.get(f"{W}styleId", "")] = {
                "parent": self._val(style.find(f"{W}basedOn")),
                "heading": int(heading.group(1)) if heading else 0 if name == "title" else self._outline(ppr),
                "toc": True if name.startswith(("toc", "оглавление")) else None,
                "num": self._num(ppr),
                # встроенные стили Word "List Bullet 2" / "List Number 3": вид и уровень списка в названии
                "list": (listed.group(1) == "number", int(listed.group(2) or 1) - 1) if listed else None,
                "bold": self._flag(rpr, "b"),
            }
        resolved: dict[str, dict] = {}
        for style_id in raw:
            merged: dict = {}
            current, depth = style_id, 0
            while current in raw and depth < 20:  # глубина ограничена на случай цикла basedOn
                for key, value in raw[current].items():
                    if merged.get(key) is None:
                        merged[key] = value
                current, depth = raw[current]["parent"], depth + 1
            resolved[style_id] = merged
        return resolved

    def _read_numbering(self) -> dict[str, dict[int, tuple[str, int]]]:
        """numId -> {уровень: (формат, начальный номер)}."""
        if "word/numbering.xml" not in self._names:
            return {}
        root = self._xml("word/numbering.xml")
        abstract: dict[str, dict[int, tuple[str, int]]] = {}
        for item in root.iter(f"{W}abstractNum"):
            levels = {}
            for level in item.iter(f"{W}lvl"):
                start = self._val(level.find(f"{W}start")) or "1"
                levels[int(level.get(f"{W}ilvl", "0"))] = (
                    self._val(level.find(f"{W}numFmt")) or "decimal", int(start) if start.isdigit() else 1)
            abstract[item.get(f"{W}abstractNumId", "")] = levels
        return {
            num.get(f"{W}numId", ""): abstract.get(self._val(num.find(f"{W}abstractNumId")) or "", {})
            for num in root.iter(f"{W}num")
        }

    # --- блоки --------------------------------------------------------------------------------

    def _blocks(self, container: ElementTree.Element) -> list[str]:
        """Содержимое тела, ячейки или надписи как список markdown-блоков; пункты списка склеиваются."""
        # (вид, уровень списка, текст); вид: "item" — пункт списка, "text" — абзац, "block" — всё остальное
        entries: list[tuple[str, int, str]] = []
        for node in self._block_nodes(container):
            if node.tag == f"{W}tbl":
                entries.extend(("block", 0, text) for text in self._table(node))
                continue
            kind, level, text, extra = self._paragraph(node)
            if text:
                entries.append((kind, level, text))
            entries.extend(("block", 0, text) for text in extra)

        out: list[str] = []
        items: list[str] = []
        for index, (kind, level, text) in enumerate(entries):
            following = entries[index + 1] if index + 1 < len(entries) else ("block", 0, "")
            if kind == "item":
                items.append(text)
            elif kind == "text" and items and following[0] == "item" and following[1] > 0:
                # абзац или картинка между вложенными пунктами — продолжение пункта, а не конец списка
                indent = INDENT * following[1]
                items.append("\n".join(indent + line for line in text.split("\n")))
            else:
                if items:
                    out.append("\n".join(items))
                    items.clear()
                out.append(text)
        if items:
            out.append("\n".join(items))
        return out

    def _block_nodes(self, container: ElementTree.Element):
        """Абзацы и таблицы в порядке документа; обёртки раскрываются, оглавление пропускается."""
        for node in container:
            if node.tag in (f"{W}p", f"{W}tbl"):
                yield node
            elif node.tag == f"{W}sdt":
                gallery = node.find(f"{W}sdtPr/{W}docPartObj/{W}docPartGallery")
                content = node.find(f"{W}sdtContent")
                if content is not None and "table of contents" not in (self._val(gallery) or "").lower():
                    yield from self._block_nodes(content)
            elif node.tag == f"{MC}AlternateContent":
                yield from self._block_nodes(self._alternate(node))
            elif node.tag in (f"{W}ins", f"{W}moveTo", f"{W}customXml", f"{W}smartTag"):
                yield from self._block_nodes(node)

    def _paragraph(self, node: ElementTree.Element) -> tuple[str, int, str, list[str]]:
        """Вернуть вид абзаца, уровень списка, Markdown и блоки надписей, привязанных к абзацу."""
        ppr = node.find(f"{W}pPr")
        style = self._styles.get(self._val(ppr.find(f"{W}pStyle")) or "" if ppr is not None else "", {})
        if style.get("toc"):
            return "text", 0, "", []
        outline = self._outline(ppr)
        heading = outline if outline is not None else style.get("heading")
        segments: list[tuple[str, bool]] = []
        extra: list[str] = []
        self._inline(node, bool(style.get("bold")), segments, extra)
        text = _format(segments, bold=heading is None)
        if not text:
            return "text", 0, "", extra
        if heading is not None:
            # "Заголовок 1" документа — это раздел; уровень "#" оставлен названию документа
            return "block", 0, f"{'#' * min(heading + 1, 6)} {text.replace(chr(10), ' ')}", extra

        listed = self._list_level(ppr, style)
        if listed is None:
            # строка текста, начинающаяся с "#" или ">", не должна стать заголовком или цитатой
            return "text", 0, re.sub(r"(?m)^([#>])", r"\\\1", text).replace("\n", HARD_BREAK), extra
        ordered, level, number = listed
        own = ORDERED_RE.match(text) if ordered else None
        if own:  # номер уже набран в тексте пункта — второй раз его не ставим
            text = f"{own.group(2)}. {(own.group(1) or '') + text[own.end():]}"
        elif ordered:
            text = f"{number}. {text}"
        else:
            text = "- " + BULLET_RE.sub("", text)
        indent = INDENT * level
        return "item", level, indent + text.replace("\n", HARD_BREAK + indent + INDENT), extra

    def _list_level(self, ppr: ElementTree.Element | None, style: dict) -> tuple[bool, int, int] | None:
        """(нумерованный ли, уровень, номер пункта) либо None, если абзац не пункт списка."""
        num = self._num(ppr) or style.get("num")
        if num is None or num[0] == "0":  # numId=0 отменяет нумерацию стиля
            named = style.get("list") if num is None else None
            if named is None:
                return None
            num_id, level, ordered, start = f"style:{named[0]}", named[1], named[0], 1
        else:
            num_id, level = num
            number_format, start = self._numbering.get(num_id, {}).get(level, ("bullet", 1))
            ordered = number_format not in UNORDERED_FORMATS
        for key in [key for key in self._counters if key[0] == num_id and key[1] > level]:
            del self._counters[key]  # пункт верхнего уровня начинает вложенную нумерацию заново
        number = self._counters[num_id, level] = self._counters.get((num_id, level), start - 1) + 1
        return ordered, min(level, 8), number

    def _table(self, node: ElementTree.Element) -> list[str]:
        rows = [[cell for cell in row if cell.tag == f"{W}tc" and cell.find(f"{W}tcPr/{W}cellDel") is None]
                for row in node if row.tag == f"{W}tr" and row.find(f"{W}trPr/{W}del") is None]
        rows = [row for row in rows if row]
        if not rows:
            return []
        if all(len(row) == 1 for row in rows):
            # таблица из одной колонки — это рамка вокруг текста (панель Confluence): выводим цитатой
            inner = [block for row in rows for block in self._blocks(row[0])]
            quoted = ["\n".join("> " + line for line in block.split("\n")) for block in inner]
            return ["\n>\n".join(quoted)] if quoted else []

        grid: list[list[str]] = []
        for row in rows:
            line: list[str] = []
            for cell in row:
                text = " <br> ".join(self._blocks(cell)).replace(HARD_BREAK, " <br> ").replace("\n", " <br> ")
                line.append(text.replace("|", "\\|"))
                span = self._val(cell.find(f"{W}tcPr/{W}gridSpan")) or "1"
                line.extend([""] * (int(span) - 1 if span.isdigit() else 0))
            grid.append(line)
        width = max(len(line) for line in grid)
        grid = [line + [""] * (width - len(line)) for line in grid]
        if not any(text for line in grid for text in line):
            return []
        md = ["| " + " | ".join(grid[0]) + " |", "|" + " --- |" * width]
        md += ["| " + " | ".join(line) + " |" for line in grid[1:]]
        return ["\n".join(md)]

    # --- строчное содержимое ------------------------------------------------------------------

    def _inline(self, node: ElementTree.Element, bold: bool, segments: list[tuple[str, bool]],
                extra: list[str]) -> None:
        """Собрать текст, ссылки и картинки абзаца в segments; надписи рядом с абзацем — в extra."""
        for kid in node:
            if kid.tag in SKIPPED:
                continue
            if kid.tag == f"{W}r":
                self._run(kid, bold, segments, extra)
            elif kid.tag == f"{W}hyperlink":
                target, external = self._targets.get(kid.get(f"{R}id", ""), ("", False))
                inner: list[tuple[str, bool]] = []
                self._inline(kid, bold, inner, extra)
                label = _format(inner).replace("\n", " ")
                if label and external and URL_RE.match(target):
                    segments.append((f"[{label}]({target})", False))
                else:
                    segments.extend(inner)
            elif kid.tag == f"{MC}AlternateContent":
                self._inline(self._alternate(kid), bold, segments, extra)
            else:  # w:ins, w:smartTag, w:sdt, w:sdtContent, w:fldSimple и другие обёртки
                self._inline(kid, bold, segments, extra)

    def _run(self, node: ElementTree.Element, bold: bool, segments: list[tuple[str, bool]],
             extra: list[str]) -> None:
        rpr = node.find(f"{W}rPr")
        explicit = self._flag(rpr, "b")
        if explicit is None and rpr is not None:
            explicit = self._styles.get(self._val(rpr.find(f"{W}rStyle")) or "", {}).get("bold")
        bold = bold if explicit is None else explicit
        for kid in node:
            if kid.tag == f"{W}t":
                segments.append((kid.text or "", bold))
            elif kid.tag == f"{W}tab":
                segments.append((" ", bold))
            elif kid.tag in (f"{W}br", f"{W}cr"):
                if kid.get(f"{W}type") not in ("page", "column"):
                    segments.append(("\n", False))
            elif kid.tag == f"{W}noBreakHyphen":
                segments.append(("-", bold))
            elif kid.tag in (f"{W}footnoteReference", f"{W}endnoteReference"):
                kind = "footnote" if kid.tag == f"{W}footnoteReference" else "endnote"
                segments.append((self._note(kind, kid.get(f"{W}id", "")), False))
            elif kid.tag in GRAPHICS:
                self._graphics(kid, segments, extra)
            elif kid.tag == f"{MC}AlternateContent":
                self._run(self._alternate(kid), bold, segments, extra)

    def _graphics(self, node: ElementTree.Element, segments: list[tuple[str, bool]], extra: list[str]) -> None:
        """Картинки рисунка встают на своё место в тексте; текст надписей идёт блоками после абзаца."""
        for kid in node:
            if kid.tag == f"{MC}AlternateContent":
                self._graphics(self._alternate(kid), segments, extra)
            elif kid.tag == f"{W}txbxContent":
                extra.extend(self._blocks(kid))
            elif kid.tag in (f"{A}blip", f"{V}imagedata"):
                marker = self._image(kid.get(f"{R}embed") or kid.get(f"{R}id") or kid.get(f"{R}link") or "")
                if marker:
                    segments.append((marker, False))
                self._graphics(kid, segments, extra)
            else:
                self._graphics(kid, segments, extra)

    def _image(self, relationship: str) -> str:
        name = self._related_part(relationship)
        if name.lower().endswith(VECTOR_SUFFIXES):
            from rag.ingestion._metafile import metafile_to_png

            if name not in self._metafiles:
                self._metafiles[name] = metafile_to_png(self._part(name), posixpath.splitext(name)[1].lower())
            return self._add_image(self._metafiles[name])
        return self._add_image(to_png(self._part(name)))

    # --- разбор XML ---------------------------------------------------------------------------

    @staticmethod
    def _alternate(node: ElementTree.Element) -> ElementTree.Element:
        """Из двух представлений одного объекта берём основное, запасное — только если основного нет."""
        choice = node.find(f"{MC}Choice")
        if choice is None:
            choice = node.find(f"{MC}Fallback")
        return choice if choice is not None else ElementTree.Element("empty")

    @staticmethod
    def _val(node: ElementTree.Element | None) -> str | None:
        return node.get(f"{W}val") if node is not None else None

    @staticmethod
    def _flag(properties: ElementTree.Element | None, name: str) -> bool | None:
        """Переключатель вида <w:b/> или <w:b w:val="0"/>; None — не задан."""
        node = properties.find(f"{W}{name}") if properties is not None else None
        return None if node is None else node.get(f"{W}val", "1") not in ("0", "false", "off")

    @staticmethod
    def _outline(ppr: ElementTree.Element | None) -> int | None:
        """Уровень заголовка из уровня структуры абзаца: 0 — "Заголовок 1"; 9 — обычный текст."""
        level = DocxConverter._val(ppr.find(f"{W}outlineLvl")) if ppr is not None else None
        return int(level) + 1 if level is not None and level.isdigit() and int(level) < 9 else None

    @staticmethod
    def _num(ppr: ElementTree.Element | None) -> tuple[str, int] | None:
        properties = ppr.find(f"{W}numPr") if ppr is not None else None
        if properties is None:
            return None
        level = DocxConverter._val(properties.find(f"{W}ilvl")) or "0"
        return DocxConverter._val(properties.find(f"{W}numId")) or "0", int(level) if level.isdigit() else 0


def _format(segments: list[tuple[str, bool]], bold: bool = True) -> str:
    """Склеить фрагменты в текст; соседние жирные фрагменты объединяются, перенос строки остаётся "\\n"."""
    runs: list[list] = []
    for text, is_bold in segments:
        is_bold = None if text == "\n" else is_bold and bold
        if runs and runs[-1][1] is is_bold:
            runs[-1][0] += text
        else:
            runs.append([text, is_bold])
    out = ""
    for text, is_bold in runs:
        if is_bold and text.strip():
            lead = text[: len(text) - len(text.lstrip())]
            trail = text[len(text.rstrip()):]
            out += f"{lead}**{text.strip()}**{trail}"
        else:
            out += text
    out = re.sub(r"[ \t\xa0]+", " ", out)
    return re.sub(r" ?\n ?", "\n", out).strip()
