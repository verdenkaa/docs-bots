"""Canonical inline S3 image links; code examples retain their original text.

Reference-style/HTML images and foreign paths are rejected explicitly. This is
a parser for the agreed raw-adapter output, not a general Markdown renderer.
"""

import re
from urllib.parse import urlsplit
from uuid import UUID, uuid5

from rag.contracts import StepError


_INLINE_IMAGE = re.compile(
    r'!\[(?:\\.|[^\]\\])*\]\([ \t]*'
    r'(?:<([^<>\n]+)>|([^\s()]+))'
    r'''(?:[ \t]+(?:"[^"\n]*"|'[^'\n]*'|\([^()\n]*\)))?[ \t]*\)'''
)


def _escaped(text, position):
    start = position
    while start > 0 and text[start - 1] == "\\":
        start -= 1
    return (position - start) % 2 == 1


def _visible_markdown(markdown):
    """Mask code and comments without shifting image replacement offsets."""
    visible = list(markdown)
    fence = None
    list_indent, blank_before = 0, False
    offset = 0
    for line in markdown.splitlines(keepends=True):
        expanded = line.expandtabs(4)
        indent = len(expanded) - len(expanded.lstrip(" "))
        marker = re.match(r" *(?:[-+*]|[0-9]{1,9}[.)])[ \t]+", expanded)
        body = expanded
        if fence is None:
            if marker and indent <= list_indent + 3:
                list_indent = marker.end()
                body = expanded[marker.end():]
            elif expanded.strip() and indent < list_indent and blank_before:
                list_indent = 0
            if not marker and list_indent and indent >= list_indent:
                body = expanded[list_indent:]
        else:
            body = expanded[fence[2]:] if indent >= fence[2] else expanded
        opening = re.match(r" {0,3}(`{3,}|~{3,})(.*)$", body.rstrip("\r\n"))
        mask = fence is not None or (bool(expanded.strip()) and indent >= list_indent + 4)
        if fence is not None:
            if re.fullmatch(r" {0,3}" + re.escape(fence[0]) + "{" + str(fence[1]) + r",}[ \t]*[\r\n]*", body):
                fence = None
        elif opening and (opening[1][0] != "`" or "`" not in opening[2]):
            fence = (opening[1][0], len(opening[1]), list_indent)
            mask = True
        if mask:
            visible[offset:offset + len(line)] = " " * len(line)
        offset += len(line)
        blank_before = not expanded.strip()
    text = "".join(visible)
    for comment in re.finditer(r"<!--[\s\S]*?(?:-->|$)", text):
        visible[comment.start():comment.end()] = " " * (comment.end() - comment.start())
    text = "".join(visible)
    position = 0
    while position < len(text):
        start = text.find("`", position)
        if start < 0:
            break
        run = re.match(r"`+", text[start:])[0]
        if _escaped(text, start):
            position = start + len(run)
            continue
        closing = re.search(r"(?<!`)" + re.escape(run) + r"(?!`)", text[start + len(run):])
        if closing is None:
            position = start + len(run)
            continue
        end = start + len(run) + closing.end()
        visible[start:end] = " " * (end - start)
        position = end
    return "".join(visible)


def _image_links(markdown: str, revision_id: UUID, bucket: str, maximum: int):
    visible = _visible_markdown(markdown)
    if re.search(r"<\s*(?:img|picture)\b", visible, re.IGNORECASE):
        raise StepError("conversion_failed", False)
    links, unique = [], {}
    position = 0
    while True:
        start = visible.find("![", position)
        if start < 0:
            break
        position = start + 2
        if _escaped(visible, start):
            continue
        match = _INLINE_IMAGE.match(visible, start)
        if match is None:
            raise StepError("conversion_failed", False)
        try:
            url = urlsplit(match[1] or match[2])
            key = re.fullmatch(r"/images/([0-9a-f-]{36})", url.path)
            if url.scheme != "s3" or url.netloc != bucket or url.query or url.fragment or key is None:
                raise ValueError()
            image_id = UUID(key[1])
            if str(image_id) != key[1]:
                raise ValueError()
            if image_id not in unique:
                if len(unique) >= maximum:
                    raise StepError("too_large", False)
                if image_id != uuid5(revision_id, "image:" + str(len(unique))):
                    raise ValueError()
                unique[image_id] = None
        except ValueError:
            raise StepError("conversion_failed", False) from None
        links.append((start, match.end(), image_id))
        position = match.end()
    return links


def _replace_images(markdown, links, images):
    descriptions = {image.image_id: image.description for image in images}
    pieces, position = [], 0
    for start, end, image_id in links:
        description = re.sub(r"([\\\[\]])", r"\\\1", descriptions[image_id])
        # The shared DTO validator counts image:// even inside the alt text.
        description = description.replace("image://", "image\\://")
        pieces.extend((markdown[position:start], f"![{description}](image://{image_id})"))
        position = end
    pieces.append(markdown[position:])
    return "".join(pieces)
