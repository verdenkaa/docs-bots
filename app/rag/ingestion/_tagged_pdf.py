"""PDF -> Markdown: внутренняя часть Document Converter.

Использует дерево структуры для заголовков, списков и таблиц, если оно есть.
Текст вне дерева добавляет по координатам; изображения извлекает независимо от
тегов, включая картинки страниц сканированного PDF. Покрытие тегами не ограничено.

Документ читается из памяти и на диск не пишется. Картинки отдаются в add_image как PNG;
строка, которую вернул add_image, ставится в том же месте текста, где картинка стоит в документе.

Зависимости (pdfplumber, pypdf) импортируются при создании конвертера, не при импорте модуля.
"""

from __future__ import annotations

import collections
import re
from collections.abc import Callable
from io import BytesIO

from rag.ingestion._parsers import ParseRefused

INLINE_TAGS = {"Span", "Link", "Reference", "Quote", "Code", "Annot", "Figure", "Formula", "Lbl"}
SKIP_TAGS = {"TOC", "TOCI"}
STANDARD_TAGS = {
    "Document", "Part", "Art", "Sect", "Div", "P", "L", "LI", "LBody", "Table", "TR", "TH", "TD",
    "THead", "TBody", "TFoot", "Caption", "BlockQuote", "Note", "H",
} | INLINE_TAGS | SKIP_TAGS | {f"H{i}" for i in range(1, 7)}
UNORDERED_NUMBERING = {"Disc", "Circle", "Square", "None"}
BULLET_RE = re.compile(r"^[•o▪■□◦·\-–]\s+")
ORDERED_RE = re.compile(r"^(\*\*)?(\d+)[.)]\s*")
RENDER_DPI = 216
LINE_BREAK_SLACK = 3.0  # пункты: запас на пробел перед словом
FIGURE = "figure"  # метка сегмента-картинки на месте признака жирности
POSITION_SLACK = 10.0  # пункты: рамка блока начинается чуть выше текста своей строки
HARD_BREAK = "  \n"  # два пробела в конце строки


class Leaf:
    """Internal marked-content position, not a boundary DTO."""

    def __init__(self, page: int, mcid: int) -> None:
        self.page, self.mcid = page, mcid


class Elem:
    """Internal PDF structure node; mutable collections belong to one parse."""

    def __init__(self, tag: str, attrs: dict | None = None, alt: str | None = None,
                 uri: str | None = None, kids: list | None = None) -> None:
        self.tag = tag
        self.attrs = {} if attrs is None else attrs
        self.alt, self.uri = alt, uri
        self.kids = [] if kids is None else kids


class TaggedPdfConverter:
    def __init__(self, data: bytes, add_image: Callable[[bytes], str]) -> None:
        import pdfplumber
        from pypdf import PdfReader

        self.stats: collections.Counter[str] = collections.Counter()
        self._add_image = add_image

        self._reader = PdfReader(BytesIO(data))
        root = self._reader.trailer["/Root"].get("/StructTreeRoot")
        root = root.get_object() if root is not None else None
        root = root if hasattr(root, "get") else {}
        rolemap = root.get("/RoleMap", {})
        rolemap = rolemap.get_object() if hasattr(rolemap, "get_object") else rolemap
        self._rolemap = {str(k)[1:]: str(v)[1:] for k, v in rolemap.items()} if isinstance(rolemap, dict) else {}
        self._page_index = {p.indirect_reference.idnum: i for i, p in enumerate(self._reader.pages)}

        # (страница, mcid) -> символы с признаком жирности / графические объекты
        self._chars: dict[tuple[int, int], list] = collections.defaultdict(list)
        self._graphics: dict[tuple[int, int], list] = collections.defaultdict(list)
        self._rasters: dict[tuple[int, int], list] = collections.defaultdict(list)
        self._raster_segments: dict[tuple[int, int], tuple] = {}
        self._image_paths: dict[int, dict[int, str | list[str]]] = {}
        self._synthetic_mcid = -1
        self._plumber = pdfplumber.open(BytesIO(data))
        try:
            for i, page in enumerate(self._plumber.pages):
                for c in page.chars:
                    if c.get("mcid") is not None:
                        position = (c["x0"], c["x1"], c["top"], i, c["bottom"])
                        self._chars[(i, c["mcid"])].append((c["text"], "bold" in c["fontname"].lower(), position))
                for kind in ("images", "curves", "lines", "rects"):
                    for o in getattr(page, kind):
                        if o.get("mcid") is not None:
                            bbox = (o["x0"], o["top"], o["x1"], o["bottom"])
                            self._graphics[(i, o["mcid"])].append((kind, bbox, o.get("name")))
                for number, image in enumerate(page.images):
                    if image.get("mcid") is not None:
                        self._rasters[(i, image["mcid"])].append((number, image))

            try:
                kids = self._build(root["/K"], None) if "/K" in root else []
            except (KeyError, TypeError, ValueError, AttributeError, RecursionError):
                # Broken optional structure must not discard readable page content.
                kids = []
            self._tree = Elem("Root", kids=kids)
            self._spans: dict[int, tuple | None] = {}
            self._complete_content()
            self._merge_split_tables(self._tree)
            self._reflow(self._tree)
        except BaseException:
            self._plumber.close()
            raise

    def convert(self) -> str:
        """Вернуть Markdown; картинки к этому моменту переданы в add_image."""
        try:
            return "\n\n".join(self._block(self._tree)) + "\n"
        finally:
            self._plumber.close()

    def _complete_content(self) -> None:
        """Добавить текст и каждое вхождение картинки, не представленные деревом."""
        reachable = {(leaf.page, leaf.mcid) for leaf in self._leaves(self._tree)}
        pictured = set()

        def figures(elem):
            if elem.tag in SKIP_TAGS:
                return
            if elem.tag == "Figure":
                for leaf in self._leaves(elem):
                    pictured.update((leaf.page, number) for number, _ in self._rasters.get((leaf.page, leaf.mcid), []))
            for kid in elem.kids:
                if isinstance(kid, Elem):
                    figures(kid)

        figures(self._tree)
        additions = []
        for page_no, page in enumerate(self._plumber.pages):
            missing = {id(char) for char in page.chars
                       if char.get("tag") != "Artifact" and (page_no, char.get("mcid")) not in reachable}
            if missing:
                # pdfplumber restores spaces/reading order from glyph coordinates.
                text_page = page.filter(lambda obj: id(obj) in missing)
                for line in text_page.extract_text_lines():
                    leaf = self._new_leaf(page_no)
                    position = (line["x0"], line["x1"], line["top"], page_no, line["bottom"])
                    self._chars[(page_no, leaf.mcid)] = [(line["text"], False, position)]
                    additions.append(Elem("P", kids=[leaf]))
            for number, image in enumerate(page.images):
                if (page_no, number) not in pictured:
                    leaf = self._new_leaf(page_no)
                    key = (page_no, leaf.mcid)
                    bbox = (image["x0"], image["top"], image["x1"], image["bottom"])
                    self._graphics[key] = [("images", bbox, image.get("name"))]
                    self._rasters[key] = [(number, image)]
                    additions.append(Elem("Figure", kids=[leaf]))
        ordered = sorted(additions, key=lambda node: self._span(node)[0])
        if self._span(self._tree) is None:
            # Entirely untagged pages need only a single ordered assembly pass.
            self._tree.kids = ordered
        else:
            for block in ordered:
                self._insert_content(self._tree, block)
        self._spans.clear()

    def _new_leaf(self, page: int) -> Leaf:
        while (page, self._synthetic_mcid) in self._chars or (page, self._synthetic_mcid) in self._graphics:
            self._synthetic_mcid -= 1
        leaf = Leaf(page, self._synthetic_mcid)
        self._synthetic_mcid -= 1
        return leaf

    def _insert_content(self, elem: Elem, block: Elem) -> bool:
        """Вставить содержимое рядом с ближайшим абзацем/ячейкой по странице и y."""
        point = self._span(block)[0]
        for kid in elem.kids:
            if not isinstance(kid, Elem) or kid.tag in SKIP_TAGS | {"Figure"}:
                continue
            span = self._span(kid)
            if span and self._not_after(span[0], point) and self._not_after(point, span[1]):
                if self._insert_content(kid, block):
                    self._spans.pop(id(elem), None)
                    return True
        if elem.tag not in {"Root", "Document", "Part", "Art", "Sect", "Div", "P", "LBody", "LI", "TD", "TH"}:
            return False
        for index, kid in enumerate(elem.kids):
            span = self._span(kid)
            if span and span[0] > point:
                elem.kids.insert(index, block)
                break
        else:
            elem.kids.append(block)
        self._spans.pop(id(elem), None)
        return True

    # ---------- дерево структуры ----------

    def _build(self, node, page: int | None) -> list:
        node = node.get_object()
        if isinstance(node, list):
            return [x for kid in node for x in self._build(kid, page)]
        if isinstance(node, int):
            return [Leaf(page, node)]
        node_type = node.get("/Type")
        if node_type == "/MCR":
            if "/Pg" in node:
                page = self._page_index[node.raw_get("/Pg").idnum]
            return [Leaf(page, int(node["/MCID"]))]
        if node_type == "/OBJR":
            return []
        if "/Pg" in node:
            page = self._page_index[node.raw_get("/Pg").idnum]

        tag = str(node.get("/S", "/Span"))[1:]
        if tag not in STANDARD_TAGS:
            tag = self._rolemap.get(tag, tag)
        elem = Elem(tag, self._attrs(node), str(node["/Alt"]) if "/Alt" in node else None)
        if tag == "Link":
            elem.uri = self._link_uri(node)
        if "/K" in node:
            elem.kids = self._build(node["/K"], page)
        return [elem]

    @staticmethod
    def _attrs(node) -> dict:
        attrs: dict = {}
        raw = node.get("/A")
        if raw is None:
            return attrs
        raw = raw.get_object()
        for item in raw if isinstance(raw, list) else [raw]:
            item = item.get_object()
            if hasattr(item, "items"):
                attrs.update({str(k)[1:]: v for k, v in item.items()})
        return attrs

    @staticmethod
    def _link_uri(node) -> str | None:
        kids = node.get("/K")
        if kids is None:
            return None
        kids = kids.get_object()
        for kid in kids if isinstance(kids, list) else [kids]:
            kid = kid.get_object()
            if hasattr(kid, "get") and kid.get("/Type") == "/OBJR":
                action = kid["/Obj"].get_object().get("/A")
                if action is not None and "/URI" in action.get_object():
                    return str(action.get_object()["/URI"])
        return None

    # ---------- порядок блоков ----------

    def _span(self, node) -> tuple | None:
        """Начало и конец узла на страницах как ((страница, y), (страница, y))."""
        key = id(node)
        if key not in self._spans:
            if isinstance(node, Leaf):
                tops = [pos[2] for _, _, pos in self._chars.get((node.page, node.mcid), [])]
                tops += [bbox[1] for _, bbox, _ in self._graphics.get((node.page, node.mcid), [])]
                points = [(node.page, top) for top in tops]
            else:
                points = [point for kid in node.kids if (span := self._span(kid)) for point in span]
            self._spans[key] = (min(points), max(points)) if points else None
        return self._spans[key]

    @staticmethod
    def _not_after(a: tuple, b: tuple) -> bool:
        return a[0] < b[0] or (a[0] == b[0] and a[1] <= b[1] + POSITION_SLACK)

    def _has_text(self, elem: Elem) -> bool:
        return any(
            text.strip() for leaf in self._leaves(elem) for text, _, _ in self._chars.get((leaf.page, leaf.mcid), [])
        )

    def _merge_split_tables(self, elem: Elem) -> None:
        """Склеивает таблицу, которую Word разбил на две на границе страниц.

        Продолжение — это следующая сразу за таблицей таблица с тем же числом колонок,
        начинающаяся на более поздней странице. Если её первая ячейка пуста, первая строка
        продолжает последнюю строку предыдущей таблицы (строка разорвана страницей).
        """
        def cells(row: Elem) -> list[Elem]:
            return [c for c in row.kids if isinstance(c, Elem) and c.tag in ("TH", "TD")]

        kids = elem.kids
        index = 0
        while index + 1 < len(kids):
            first, second = kids[index], kids[index + 1]
            if not all(isinstance(t, Elem) and t.tag == "Table" for t in (first, second)):
                index += 1
                continue
            rows, more = list(self._rows(first)), list(self._rows(second))
            spans = self._span(first), self._span(second)
            if not (rows and more and all(spans) and spans[1][0][0] > spans[0][0][0]
                    and len(cells(rows[-1])) == len(cells(more[0])) > 1):
                index += 1
                continue
            if not self._has_text(cells(more[0])[0]):
                for target, source in zip(cells(rows[-1]), cells(more[0])):
                    target.kids.extend(source.kids)
                more = more[1:]
            first.kids.extend(more)
            del kids[index + 1]
            self._spans.clear()
            self.stats["tables_merged_across_pages"] += 1
        for node in kids:
            if isinstance(node, Elem):
                self._merge_split_tables(node)

    def _reflow(self, elem: Elem) -> None:
        """Возвращает внутрь списка блоки, которые стоят между его пунктами.

        Word записывает таблицу, рамку или картинку, разрывающую список, после всего списка,
        хотя на странице она стоит между пунктами. Такой блок виден по координатам: он
        начинается раньше последнего пункта списка. Переносим его в пункт, под которым он стоит.
        """
        kids = elem.kids
        for index, node in enumerate(kids):
            if not (isinstance(node, Elem) and node.tag == "L"):
                continue
            item_starts = [span[0] for item in self._items(node) if (span := self._span(item))]
            last_item_start = max(item_starts, default=None)
            cursor = index + 1
            while cursor < len(kids):
                block = kids[cursor]
                if not isinstance(block, Elem) or block.tag == "L" or re.fullmatch(r"H[1-6]?", block.tag):
                    break
                span = self._span(block)
                # блок стоит между пунктами, только если после него начинается ещё хотя бы один пункт
                inside = bool(span) and last_item_start is not None and span[0] < last_item_start
                item = self._item_above(node, span[0]) if inside else None
                if item is None:
                    cursor += 1
                    continue
                body = next((k for k in item.kids if isinstance(k, Elem) and k.tag == "LBody"), item)
                at = len(body.kids)
                for position, kid in enumerate(body.kids):
                    kid_span = self._span(kid)
                    if kid_span and not self._not_after(kid_span[0], span[0]):
                        at = position
                        break
                body.kids.insert(at, kids.pop(cursor))
                self.stats["blocks_moved_into_lists"] += 1
        for node in kids:
            if isinstance(node, Elem):
                self._reflow(node)

    def _item_above(self, list_elem: Elem, point: tuple) -> Elem | None:
        """Последний пункт списка (любой вложенности), который начинается не ниже точки."""
        best = None
        for item in self._items(list_elem):
            span = self._span(item)
            if span and self._not_after(span[0], point):
                best = item
        return best

    def _items(self, list_elem: Elem):
        for kid in list_elem.kids:
            if isinstance(kid, Elem):
                if kid.tag == "LI":
                    yield kid
                yield from self._items(kid)

    # ---------- строчное содержимое ----------

    def _leaves(self, elem: Elem):
        for kid in elem.kids:
            if isinstance(kid, Leaf):
                yield kid
            else:
                yield from self._leaves(kid)

    def _segments(self, node) -> list[tuple]:
        """Содержимое узла как [(текст, жирный, (x0, x1, top, страница, bottom))]; картинки и ссылки — готовым markdown."""
        if isinstance(node, Leaf):
            return list(self._chars.get((node.page, node.mcid), []))
        if node.tag in SKIP_TAGS:
            return []
        if node.tag == "Figure":
            images = [(leaf.page, number, image) for leaf in self._leaves(node)
                      for number, image in self._rasters.get((leaf.page, leaf.mcid), [])]
            if images:
                text = [segment for leaf in self._leaves(node)
                        for segment in self._chars.get((leaf.page, leaf.mcid), [])]
                return text + [self._raster(page, number, image) for page, number, image in images]
            if any(self._graphics.get((leaf.page, leaf.mcid)) for leaf in self._leaves(node)):
                return [self._figure(node)]
        segments = [s for kid in node.kids for s in self._segments(kid)]
        if node.tag == "Link" and node.uri:
            text = "".join(t for t, _, _ in segments).strip()
            placed = [pos for _, _, pos in segments if pos]
            position = (placed[0][0], placed[-1][1], *placed[0][2:]) if placed else None
            return [(f"[{text}]({node.uri})", None, position)] if text else []
        return segments

    @staticmethod
    def _place_figures(segments) -> list:
        """Ставит картинки абзаца туда, где они стоят на странице.

        Word записывает картинки в теги после всего текста абзаца, даже если картинка стоит
        посреди строки или над текстом. Место определяем по координатам: в строке — между
        соседними символами, на отдельной строке — перед текстом, который идёт ниже.
        """
        figures = [seg for seg in segments if seg[1] is FIGURE and seg[2]]
        if not figures:
            return segments
        placed = [seg for seg in segments if not (seg[1] is FIGURE and seg[2])]
        for figure in figures:
            x0, x1, top, page, bottom = figure[2]
            text = [(i, seg[2]) for i, seg in enumerate(placed) if seg[2] and seg[1] is not FIGURE]
            same_line = [(i, pos) for i, pos in text if pos[3] == page and top - 1 <= (pos[2] + pos[4]) / 2 <= bottom + 3]
            if same_line:
                right = [i for i, pos in same_line if pos[0] >= (x0 + x1) / 2]
                at = right[0] if right else same_line[-1][0] + 1
            else:
                below = [i for i, pos in text if pos[3] > page or (pos[3] == page and pos[2] >= bottom - 1)]
                at = below[0] if below else len(placed)
            placed.insert(at, figure)
        return placed

    @staticmethod
    def _figure_breaks(segments) -> set[int]:
        """Переносы строки вокруг картинок, которые стоят на отдельной строке, а не в тексте."""
        def other_line(figure, position) -> bool:
            center = (position[2] + position[4]) / 2
            return position[3] != figure[3] or not figure[2] - 1 <= center <= figure[4] + 3

        breaks = set()
        positioned = [(i, seg) for i, seg in enumerate(segments) if seg[2]]
        for order, (index, seg) in enumerate(positioned):
            if seg[1] is not FIGURE:
                continue
            before = positioned[order - 1] if order else None
            after = positioned[order + 1] if order + 1 < len(positioned) else None
            if before and before[1][1] is not FIGURE and other_line(seg[2], before[1][2]):
                breaks.add(index)
            if after and after[1][1] is not FIGURE and other_line(seg[2], after[1][2]):
                breaks.add(after[0])
        return breaks

    @staticmethod
    def _explicit_breaks(segments) -> set[int]:
        """Номера сегментов, перед которыми автор поставил перенос строки внутри абзаца.

        Обычный перенос по ширине отличается тем, что первое слово новой строки
        не поместилось бы в остаток предыдущей; если поместилось бы — перенос явный.
        Шириной считается самая длинная строка того же абзаца: настоящая ширина колонки
        неизвестна, поэтому перенос после самой длинной строки не распознаётся.
        """
        lines: list[dict] = []
        previous = None
        for index, (text, _, position) in enumerate(segments):
            if position is None:
                continue
            x0, x1, top, page, _ = position
            if previous is None or page != previous[3] or (x0 < previous[1] - 1 and top > previous[2] + 2):
                lines.append({"start": index, "end": x0, "page": page,
                              "word_from": None, "word_to": None, "word_done": False})
            line = lines[-1]
            if text.strip(" \t\n"):  # неразрывный пробел слово не разбивает
                line["end"] = max(line["end"], x1)
                if not line["word_done"]:
                    line["word_from"] = x0 if line["word_from"] is None else line["word_from"]
                    line["word_to"] = x1
            elif line["word_from"] is not None:
                line["word_done"] = True
            previous = position
        if len(lines) < 2:
            return set()
        right_edge = max(line["end"] for line in lines)
        breaks = set()
        for before, line in zip(lines, lines[1:]):
            if line["word_from"] is None:
                continue
            first_word = line["word_to"] - line["word_from"]
            if right_edge - before["end"] > first_word + LINE_BREAK_SLACK:
                breaks.add(line["start"])
        return breaks

    def _format(self, segments, bold: bool = True) -> str:
        """Склеивает сегменты в текст; явные переносы строки остаются как "\n"."""
        segments = self._place_figures(segments)
        breaks = self._explicit_breaks(segments) | self._figure_breaks(segments)
        runs: list[list] = []
        for index, (text, is_bold, _) in enumerate(segments):
            if index in breaks:
                runs.append(["\n", None])
            is_bold = is_bold is True and bold
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

    def _figure(self, elem: Elem) -> tuple:
        graphics = [
            (leaf.page, kind, bbox, name)
            for leaf in self._leaves(elem)
            for kind, bbox, name in self._graphics.get((leaf.page, leaf.mcid), [])
        ]
        if not graphics:
            raise ParseRefused("unsupported_format", "PDF figure has no extractable graphics")
        page = graphics[0][0]
        on_page = [g for g in graphics if g[0] == page]
        images = [g for g in on_page if g[1] == "images"]

        boxes = [g[2] for g in (images or on_page)]
        bbox = (
            min(b[0] for b in boxes), min(b[1] for b in boxes),
            max(b[2] for b in boxes), max(b[3] for b in boxes),
        )
        png = self._original_png(page, images[0][3]) if len(images) == 1 else None
        if png is None:
            target = BytesIO()
            self._plumber.pages[page].crop(bbox, strict=False).to_image(resolution=RENDER_DPI).save(target, format="PNG")
            png = target.getvalue()
            self.stats["images_rendered"] += 1
        # подпись из /Alt (имя файла картинки) не нужна: описание даёт Model Gateway
        return (self._add_image(png), FIGURE, (bbox[0], bbox[2], bbox[1], page, bbox[3]))

    def _original_png(self, page: int, xobject_name: str | list[str] | None) -> bytes | None:
        """Достаёт исходное изображение из PDF без перерисовки страницы."""
        if not xobject_name:
            return None
        try:
            key = xobject_name if isinstance(xobject_name, list) else "/" + xobject_name.lstrip("/")
            image = self._reader.pages[page].images[key].image
            if image.mode not in ("RGB", "RGBA", "L", "LA", "P"):
                image = image.convert("RGB")
            target = BytesIO()
            image.save(target, format="PNG")
        except Exception:  # noqa: BLE001 - любой сбой декодирования уводит на перерисовку области
            return None
        self.stats["images_original"] += 1
        return target.getvalue()

    def _raster_path(self, page: int, image: dict) -> str | list[str] | None:
        """Разрешить объект, включая Form XObject с повторяющимися локальными именами."""
        object_id = getattr(image.get("stream"), "objid", None)
        if object_id is None:
            return None  # inline image: render its visible rectangle
        if page not in self._image_paths:
            paths = {}
            source = self._reader.pages[page]
            try:
                for path in source.images.keys():
                    if isinstance(path, str) and path.startswith("~"):
                        continue
                    obj = source
                    for name in path if isinstance(path, list) else [path]:
                        obj = obj["/Resources"]["/XObject"][name]
                    reference = getattr(obj, "indirect_reference", None)
                    if reference is not None:
                        paths[reference.idnum] = path
            except (KeyError, TypeError, ValueError, AttributeError):
                # The page renderer can still handle an image the resource walker cannot.
                pass
            self._image_paths[page] = paths
        return self._image_paths[page].get(object_id)

    def _raster(self, page: int, number: int, image: dict) -> tuple:
        """Отдельный плейсхолдер для каждого нарисованного вхождения изображения."""
        key = (page, number)
        if key not in self._raster_segments:
            bbox = (image["x0"], image["top"], image["x1"], image["bottom"])
            png = self._original_png(page, self._raster_path(page, image))
            if png is None:
                target = BytesIO()
                self._plumber.pages[page].crop(bbox, strict=False).to_image(resolution=RENDER_DPI).save(target, format="PNG")
                png = target.getvalue()
                self.stats["images_rendered"] += 1
            self._raster_segments[key] = (self._add_image(png), FIGURE,
                                          (bbox[0], bbox[2], bbox[1], page, bbox[3]))
        return self._raster_segments[key]

    # ---------- блоки ----------

    def _blocks(self, elem: Elem) -> list[str]:
        """Дети узла как список markdown-блоков; подряд идущее строчное содержимое склеивается в абзац."""
        out: list[str] = []
        buffer: list = []

        def flush() -> None:
            if buffer:
                text = self._format(buffer)
                if text:
                    # явный перенос строки остаётся внутри абзаца как жёсткий перенос markdown
                    # строка текста, начинающаяся с "#" или ">", не должна стать заголовком или цитатой
                    text = re.sub(r"(?m)^([#>])", r"\\\1", text)
                    out.append(text.replace("\n", HARD_BREAK))
                buffer.clear()

        for kid in elem.kids:
            if isinstance(kid, Leaf) or kid.tag in INLINE_TAGS:
                buffer.extend(self._segments(kid))
                continue
            flush()
            out.extend(self._block(kid))
        flush()
        return out

    def _block(self, elem: Elem) -> list[str]:
        if elem.tag in SKIP_TAGS:
            return []
        heading = re.fullmatch(r"H([1-6])", elem.tag)
        if heading:
            text = self._format(self._segments(elem), bold=False).replace("\n", " ")
            self.stats["headings"] += bool(text)
            # H1 документа — это раздел; уровень "#" оставлен названию документа
            return [f"{'#' * min(int(heading.group(1)) + 1, 6)} {text}"] if text else []
        if elem.tag == "L":
            return self._list(elem)
        if elem.tag == "Table":
            return self._table(elem)
        return self._blocks(elem)

    def _list(self, elem: Elem) -> list[str]:
        ordered = str(elem.attrs.get("ListNumbering", "/Disc"))[1:] not in UNORDERED_NUMBERING
        lines: list[str] = []
        for item in elem.kids:
            if isinstance(item, Leaf):
                continue
            if item.tag == "LI":  # LI = [Lbl] + LBody: содержимое LBody поднимаем на уровень пункта
                flat = Elem("LI")
                for kid in item.kids:
                    flat.kids.extend(kid.kids if isinstance(kid, Elem) and kid.tag == "LBody" else [kid])
                blocks = self._blocks(flat)
            else:
                blocks = self._block(item)
            if not blocks:
                continue
            head = blocks[0]
            numbered = ORDERED_RE.match(head) if ordered else None
            if numbered:
                head = f"{numbered.group(2)}. {(numbered.group(1) or '') + head[numbered.end():]}"
            else:  # буквенная и римская нумерация остаётся текстом пункта: "- a. ..."
                head = "- " + (head if ordered else BULLET_RE.sub("", head))
            lines.append(head.replace("\n", "\n    "))
            for extra in blocks[1:]:
                lines.append("\n".join("    " + line for line in extra.split("\n")))
        self.stats["lists"] += 1
        return ["\n".join(lines)] if lines else []

    def _rows(self, elem: Elem):
        for kid in elem.kids:
            if isinstance(kid, Elem):
                if kid.tag == "TR":
                    yield kid
                elif kid.tag in ("THead", "TBody", "TFoot"):
                    yield from self._rows(kid)

    def _table(self, elem: Elem) -> list[str]:
        rows = [[c for c in row.kids if isinstance(c, Elem) and c.tag in ("TH", "TD")] for row in self._rows(elem)]
        rows = [row for row in rows if row]
        if not rows:
            return self._blocks(elem)
        if all(len(row) == 1 for row in rows):
            # таблица из одной колонки — это рамка вокруг текста (панель Confluence): выводим цитатой
            inner = [block for row in rows for block in self._blocks(row[0])]
            self.stats["callouts"] += 1
            quoted = ["\n".join("> " + line for line in block.split("\n")) for block in inner]
            return ["\n>\n".join(quoted)] if quoted else []

        grid: list[list[str]] = []
        for row in rows:
            line: list[str] = []
            for cell in row:
                text = " <br> ".join(self._blocks(cell)).replace(HARD_BREAK, " <br> ").replace("\n", " <br> ")
                line.append(text.replace("|", "\\|"))
                line.extend([""] * (int(cell.attrs.get("ColSpan", 1)) - 1))
            grid.append(line)
        width = max(len(line) for line in grid)
        grid = [line + [""] * (width - len(line)) for line in grid]
        md = ["| " + " | ".join(grid[0]) + " |", "|" + " --- |" * width]
        md += ["| " + " | ".join(line) + " |" for line in grid[1:]]
        self.stats["tables"] += 1
        return ["\n".join(md)]
