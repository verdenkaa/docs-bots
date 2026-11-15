"""DOCX: headers/footers, inline notes, accepted revisions and native metafiles."""

import asyncio
import contextlib
import io
import os
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock
from uuid import UUID

from sample_docx import _png, build_metafile

from rag.contracts import StepError
from rag.ingestion import converter
from rag.ingestion._config import ConverterSettings
from rag.ingestion._parsers import DocxParser, ParseRefused
from rag.ingestion.contracts import UploadInput, UploadSource
from rag.model_gateway.contracts import ImageDescription, OcrResult

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = f'xmlns:w="{W}" xmlns:r="{R}" xmlns:a="{A}"'
HAS_RENDERER = hasattr(os, "memfd_create") and shutil.which("inkscape") is not None


def run(text):
    return f"<w:r><w:t>{text}</w:t></w:r>"


def paragraph(content):
    return f"<w:p>{content}</w:p>"


def image(reference="image"):
    return f'<w:r><w:drawing><a:blip r:embed="{reference}"/></w:drawing></w:r>'


def relationships(*items):
    return f'<Relationships xmlns="{REL}">' + "".join(
        f'<Relationship Id="{reference}" Target="{target}" TargetMode="{mode}"/>'
        for reference, target, mode in items) + "</Relationships>"


def document(body, parts=None):
    target = io.BytesIO()
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("word/document.xml", f"<w:document {NS}><w:body>{body}</w:body></w:document>")
        for name, data in (parts or {}).items():
            archive.writestr(name, data)
    return target.getvalue()


def parse(data):
    images = []

    def add_image(png):
        images.append(png)
        return f"IMAGE-{len(images)}"

    return DocxParser().parse(data, add_image), images


class DocxParserTests(unittest.TestCase):
    def test_headers_and_footers_are_placed_around_their_sections_once(self):
        first = '<w:sectPr><w:headerReference r:id="h1"/><w:footerReference r:id="f1"/></w:sectPr>'
        second = '<w:sectPr><w:headerReference r:id="h1"/><w:headerReference r:id="h2"/></w:sectPr>'
        body = f"<w:p><w:pPr>{first}</w:pPr>{run('Section one')}</w:p>"
        body += paragraph(run("Section two")) + second
        parts = {"word/_rels/document.xml.rels": relationships(
            ("h1", "header1.xml", "Internal"), ("h2", "header2.xml", "Internal"),
            ("f1", "footer1.xml", "Internal")),
            "word/header1.xml": f"<w:hdr {NS}>{paragraph(run('Header one'))}</w:hdr>",
            "word/header2.xml": f"<w:hdr {NS}>{paragraph(run('Header two'))}</w:hdr>",
            "word/footer1.xml": f"<w:ftr {NS}>{paragraph(run('Footer one'))}</w:ftr>",
            "word/header-unused.xml": f"<w:hdr {NS}>{paragraph(run('Unused'))}</w:hdr>"}
        markdown, _ = parse(document(body, parts))
        self.assertEqual(markdown, "Header one\n\nSection one\n\nFooter one\n\nHeader two\n\nSection two\n")

    def test_footnotes_and_endnotes_follow_the_reference_in_parentheses(self):
        body = paragraph(run("Five days") + '<w:r><w:footnoteReference w:id="1"/></w:r>' + run(" before departure."))
        body += paragraph(run("Details") + '<w:r><w:endnoteReference w:id="2"/></w:r>')
        parts = {"word/footnotes.xml": f'<w:footnotes {NS}>'
                 '<w:footnote w:type="separator" w:id="-1"><w:p/></w:footnote>'
                 f'<w:footnote w:id="1">{paragraph(run("Working days only"))}'
                 f'{paragraph(run("Exclude holidays"))}</w:footnote></w:footnotes>',
                 "word/endnotes.xml": f'<w:endnotes {NS}><w:endnote w:id="2">'
                 f'{paragraph(run("See the annex"))}</w:endnote></w:endnotes>'}
        markdown, _ = parse(document(body, parts))
        self.assertEqual(markdown, "Five days (Working days only; Exclude holidays) before departure.\n\n"
                         "Details (See the annex)\n")

    def test_part_relationships_are_restored_after_headers_notes_and_footers(self):
        body = paragraph('<w:hyperlink r:id="link">' + run("Body") + '</w:hyperlink>'
                         '<w:r><w:footnoteReference w:id="1"/></w:r>' + image())
        body += '<w:sectPr><w:headerReference r:id="h"/><w:footerReference r:id="f"/></w:sectPr>'
        content = paragraph('<w:hyperlink r:id="link">' + run("Part") + '</w:hyperlink>' + image())
        parts = {"word/_rels/document.xml.rels": relationships(
            ("link", "https://example.com/body", "External"), ("image", "media/body.png", "Internal"),
            ("h", "headers/header.xml", "Internal"), ("f", "footer.xml", "Internal")),
            "word/headers/header.xml": f"<w:hdr {NS}>{content}</w:hdr>",
            "word/headers/_rels/header.xml.rels": relationships(
                ("link", "https://example.com/header", "External"), ("image", "../media/header.png", "Internal")),
            "word/footer.xml": f"<w:ftr {NS}>{content}</w:ftr>",
            "word/_rels/footer.xml.rels": relationships(
                ("link", "https://example.com/footer", "External"), ("image", "media/footer.png", "Internal")),
            "word/footnotes.xml": f'<w:footnotes {NS}><w:footnote w:id="1">{content}</w:footnote></w:footnotes>',
            "word/_rels/footnotes.xml.rels": relationships(
                ("link", "https://example.com/note", "External"), ("image", "media/note.png", "Internal"))}
        parts.update({"word/media/" + name + ".png": _png() for name in ("body", "header", "footer", "note")})
        markdown, images = parse(document(body, parts))
        self.assertEqual(markdown, "[Part](https://example.com/header)IMAGE-1\n\n"
                         "[Body](https://example.com/body) ([Part](https://example.com/note)IMAGE-2)IMAGE-3\n\n"
                         "[Part](https://example.com/footer)IMAGE-4\n")
        self.assertEqual(len(images), 4)
        self.assertTrue(all(png.startswith(b"\x89PNG\r\n\x1a\n") for png in images))

    def test_only_current_text_and_table_rows_and_cells_are_used(self):
        body = paragraph(run("Current ") + '<w:del>' + run("deleted") + '</w:del>'
                         '<w:ins>' + run("inserted") + '</w:ins><w:moveFrom>' + run("old location")
                         + '</w:moveFrom><w:moveTo>' + run(" moved") + '</w:moveTo>')
        row = lambda text: '<w:tc>' + paragraph(run(text)) + '</w:tc>'
        body += '<w:tbl><w:tr>' + row("Name") + row("Value") + '</w:tr>'
        body += '<w:tr><w:trPr><w:del/></w:trPr>' + row("Deleted row") + row("Obsolete") + '</w:tr>'
        body += '<w:tr><w:tc><w:tcPr><w:cellDel/></w:tcPr>' + paragraph(run("Deleted cell")) + '</w:tc>'
        body += row("Active") + row("New") + '</w:tr></w:tbl>'
        markdown, _ = parse(document(body))
        self.assertEqual(markdown, "Current inserted moved\n\n| Name | Value |\n| --- | --- |\n| Active | New |\n")

    def test_missing_or_recursive_notes_are_refused(self):
        reference = '<w:r><w:footnoteReference w:id="1"/></w:r>'
        body = paragraph(run("Text") + reference)
        recursive = f'<w:footnotes {NS}><w:footnote w:id="1">{paragraph(reference)}</w:footnote></w:footnotes>'
        for parts in ({}, {"word/footnotes.xml": recursive}):
            with self.subTest(parts=bool(parts)), self.assertRaises(ParseRefused) as caught:
                parse(document(body, parts))
            self.assertEqual(caught.exception.code, "conversion_failed")

    def test_external_images_still_refuse_the_document_in_all_parts(self):
        for part in ("document", "header", "footnote"):
            parts = {}
            content = paragraph(image())
            if part == "document":
                body, rel_part = content, "word/_rels/document.xml.rels"
            elif part == "header":
                body = paragraph(run("Body")) + '<w:sectPr><w:headerReference r:id="h"/></w:sectPr>'
                parts["word/_rels/document.xml.rels"] = relationships(("h", "header.xml", "Internal"))
                parts["word/header.xml"] = f"<w:hdr {NS}>{content}</w:hdr>"
                rel_part = "word/_rels/header.xml.rels"
            else:
                body = paragraph(run("Body") + '<w:r><w:footnoteReference w:id="1"/></w:r>')
                parts["word/footnotes.xml"] = f'<w:footnotes {NS}><w:footnote w:id="1">{content}</w:footnote></w:footnotes>'
                rel_part = "word/_rels/footnotes.xml.rels"
            parts[rel_part] = relationships(("image", "https://example.invalid/picture.png", "External"))
            with self.subTest(part=part), self.assertRaises(ParseRefused) as caught:
                parse(document(body, parts))
            self.assertEqual(caught.exception.code, "unsupported_format")

    @unittest.skipUnless(HAS_RENDERER, "нужны Linux memfd и Inkscape")
    def test_real_emf_wmf_and_nonplaceable_wmf_render_with_color_and_size_bound(self):
        from PIL import Image
        from rag.ingestion import _metafile

        for kind, data in (("emf", build_metafile("emf")), ("wmf", build_metafile("wmf")),
                           ("wmf", build_metafile("wmf")[22:])):
            with self.subTest(kind=kind, size=len(data)), mock.patch.object(_metafile, "MAX_SIDE", 64):
                png = _metafile.metafile_to_png(data, "." + kind)
            with Image.open(io.BytesIO(png)) as rendered:
                self.assertLessEqual(max(rendered.size), 64)
                self.assertGreater(rendered.width, rendered.height)
                red, green, blue = rendered.convert("RGB").getpixel((rendered.width // 2, rendered.height // 2))
                self.assertGreater(red, 200)
                self.assertLess(green, 50)
                self.assertLess(blue, 50)

    def test_bad_metafile_is_refused_and_missing_renderer_is_configuration_error(self):
        from rag.ingestion._metafile import metafile_to_png

        with self.assertRaises(ParseRefused):
            metafile_to_png(b"not an EMF", ".emf")
        with mock.patch("rag.ingestion._metafile.shutil.which", return_value=None), self.assertRaises(ImportError):
            metafile_to_png(build_metafile("emf"), ".emf")


@unittest.skipUnless(HAS_RENDERER, "нужны Linux memfd и Inkscape")
class DocxMetafileConversionTests(unittest.IsolatedAsyncioTestCase):
    async def test_emf_and_wmf_reach_ocr_vision_and_become_placeholders(self):
        parts = {"word/_rels/document.xml.rels": relationships(
            ("emf", "media/image.emf", "Internal"), ("wmf", "media/image.wmf", "Internal")),
            "word/media/image.emf": build_metafile("emf"), "word/media/image.wmf": build_metafile("wmf")}
        data = document(paragraph(run("Diagram ") + image("emf")) + paragraph(image("wmf")), parts)

        async def stream():
            yield data

        upload = UploadInput(content=stream(), source=UploadSource(filename="vector.docx", access_level="employees"),
                             document_id=None)
        describe = mock.AsyncMock(return_value=ImageDescription(profile_id="vision", description="Red rectangle"))
        recognize = mock.AsyncMock(return_value=OcrResult(profile_id="ocr", text="Rectangle"))
        with (mock.patch.object(converter, "converter_settings", return_value=ConverterSettings(_env_file=None)),
              mock.patch.object(converter.model_gateway, "describe_image", describe),
              mock.patch.object(converter.model_gateway, "recognize_text", recognize),
              contextlib.redirect_stdout(io.StringIO())):
            result = await converter.convert_document(upload, UUID("7d0a2a4e-58f5-4e3c-9d5e-3f2b9c1a6b10"))
        self.assertEqual(len(result.images), 2)
        for converted in result.images:
            self.assertEqual(converted.content.mime_type, "image/png")
            self.assertIn(f"![Red rectangle. Текст на изображении: Rectangle](image://{converted.ref.image_id})",
                          result.markdown)
        expected = {converted.content.data for converted in result.images}
        self.assertEqual({call.args[0].data for call in describe.call_args_list}, expected)
        self.assertEqual({call.args[0].data for call in recognize.call_args_list}, expected)

    async def test_renderer_and_workspace_are_cleaned_on_timeout_and_cancellation(self):
        from rag.ingestion import _document_parser

        factory = tempfile.TemporaryDirectory
        for cancel in (False, True):
            with self.subTest(cancel=cancel), factory() as root:
                root = Path(root)
                pid_file = root / "pids"
                fake = root / "inkscape"
                fake.write_text(f"#!{sys.executable}\nimport os, subprocess, sys, time\n"
                                "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
                                f"open({str(pid_file)!r}, 'w').write(str(os.getpid()) + ' ' + str(child.pid))\n"
                                "time.sleep(60)\n")
                fake.chmod(0o755)
                workspaces = []

                def workspace(*args, **kwargs):
                    created = factory(*args, **kwargs, dir=root)
                    workspaces.append(Path(created.name))
                    return created

                data = document(paragraph(image()), {
                    "word/_rels/document.xml.rels": relationships(("image", "media/image.emf", "Internal")),
                    "word/media/image.emf": build_metafile("emf")})
                settings = ConverterSettings(_env_file=None, parse_timeout_s=5)
                with (mock.patch.dict(os.environ, {"PATH": str(root)}),
                      mock.patch.object(_document_parser.tempfile, "TemporaryDirectory", side_effect=workspace)):
                    task = asyncio.create_task(_document_parser._run_worker("docx", data, "IMAGE-{}", settings))
                    async with asyncio.timeout(4):
                        while not pid_file.exists():
                            await asyncio.sleep(0.01)
                    if cancel:
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task
                    else:
                        with self.assertRaises(StepError) as caught:
                            await task
                        self.assertEqual((caught.exception.code, caught.exception.retryable),
                                         ("converter_unavailable", True))
                self.assertTrue(workspaces)
                self.assertTrue(all(not path.exists() for path in workspaces))
                self.assertTrue(pid_file.exists())
                for pid in pid_file.read_text().split():
                    status = Path(f"/proc/{pid}/stat")
                    self.assertTrue(not status.exists() or status.read_text().split()[2] == "Z")
