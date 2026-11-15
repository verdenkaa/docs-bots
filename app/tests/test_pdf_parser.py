"""Real PDFs: optional tags, orphan content, illustrations and scan refusals."""

from copy import deepcopy
import importlib.util
from io import BytesIO
import unittest
from unittest.mock import patch

import sample_pdf
from rag.ingestion._parsers import PARSERS, ParseRefused


HAS_PDF_LIBS = all(importlib.util.find_spec(name) for name in ("pdfplumber", "pypdf"))


@unittest.skipUnless(HAS_PDF_LIBS, "нужны pdfplumber и pypdf")
class PdfParserTests(unittest.TestCase):
    def parse(self, data):
        images = []
        def add_image(png):
            marker = f"IMAGE-{len(images)}"
            images.append(png)
            return marker
        markdown = PARSERS["pdf"].parse(data, add_image)
        return markdown, images

    def test_short_untagged_condition_is_preserved_with_tagged_structure(self):
        text, images = self.parse(sample_pdf.build_sample_pdf(untagged_text="!> 200"))
        self.assertIn("!> 200", text)
        self.assertIn("## 1 Getting started", text)
        self.assertEqual(len(images), 1)

    def test_low_tag_coverage_never_rejects_or_discards_page_text(self):
        content = b"Outside tags " * 150
        text, images = self.parse(sample_pdf.build_sample_pdf(untagged_text=content.decode()))
        self.assertEqual(text.count("Outside tags"), 150)
        self.assertIn("Plain text and **bold text** in one paragraph.", text)
        self.assertEqual(len(images), 1)

    def test_missing_and_empty_structure_both_preserve_unmarked_page(self):
        content = b"BT /F1 11 Tf 50 700 Td (Plain text !> 200) Tj ET"
        for tagged in (False, True):
            with self.subTest(tagged=tagged):
                text, images = self.parse(sample_pdf.build_sample_pdf(tagged=tagged, page_content=content))
                self.assertEqual(text, "Plain text !> 200\n")
                self.assertEqual(images, [])

    def test_orphan_mcid_text_is_preserved(self):
        content = b"/P <</MCID 999>> BDC BT /F1 11 Tf 50 700 Td (Orphan condition !> 200) Tj ET EMC"
        text, images = self.parse(sample_pdf.build_sample_pdf(page_content=content))
        self.assertEqual(text, "Orphan condition !> 200\n")
        self.assertEqual(images, [])

    def test_long_document_without_structural_headings_is_accepted(self):
        paragraph = "word " * 900
        structure = ("Document", {}, [("P", {}, [0])])
        content = f"/P <</MCID 0>> BDC BT /F1 11 Tf 50 700 Td ({paragraph}) Tj ET EMC".encode()
        with patch.object(sample_pdf, "_STRUCTURE", structure):
            data = sample_pdf.build_sample_pdf(page_content=content)
        text, images = self.parse(data)
        self.assertEqual(text.count("word"), 900)
        self.assertEqual(images, [])

    def test_image_missing_from_structure_is_still_extracted_once(self):
        structure = deepcopy(sample_pdf._STRUCTURE)
        paragraph = next(kid for kid in structure[2] if isinstance(kid, tuple)
                         and any(isinstance(child, tuple) and child[0] == "Figure" for child in kid[2]))
        paragraph[2][:] = [child for child in paragraph[2] if not isinstance(child, tuple)]
        with patch.object(sample_pdf, "_STRUCTURE", structure):
            data = sample_pdf.build_sample_pdf()
        text, images = self.parse(data)
        self.assertEqual(len(images), 1)
        self.assertEqual(text.count("IMAGE-0"), 1)
        self.assertIn("Press IMAGE-0 to save.", text)

    def test_image_inside_skipped_toc_is_still_extracted(self):
        structure = ("Document", {}, [("TOC", {}, [("Figure", {}, [17])])])
        with patch.object(sample_pdf, "_STRUCTURE", structure):
            data = sample_pdf.build_sample_pdf()
        text, images = self.parse(data)
        self.assertEqual(len(images), 1)
        self.assertEqual(text.count("IMAGE-0"), 1)

    def test_one_figure_with_multiple_rasters_keeps_each_occurrence(self):
        structure = ("Document", {}, [("P", {}, [("Figure", {}, [17])])])
        content = (b"BT /F1 11 Tf 50 700 Td (Pictures) Tj ET\n"
                   b"/Figure <</MCID 17>> BDC q 20 0 0 20 50 600 cm /Im1 Do Q q 20 0 0 20 50 500 cm /Im1 Do Q EMC")
        with patch.object(sample_pdf, "_STRUCTURE", structure):
            data = sample_pdf.build_sample_pdf(page_content=content)
        text, images = self.parse(data)
        self.assertEqual(len(images), 2)
        self.assertEqual(text.count("IMAGE-0"), 1)
        self.assertEqual(text.count("IMAGE-1"), 1)

    def test_text_inside_raster_figure_is_not_discarded(self):
        structure = ("Document", {}, [("P", {}, [("Figure", {}, [17, 18])])])
        with patch.object(sample_pdf, "_STRUCTURE", structure):
            data = sample_pdf.build_sample_pdf()
        text, images = self.parse(data)
        self.assertEqual(len(images), 1)
        self.assertEqual(text.count("to save."), 1)
        self.assertEqual(text.count("IMAGE-0"), 1)

    def test_inline_and_artifact_images_are_extracted_without_figure_tags(self):
        from PIL import Image

        content = (b"BT /F1 11 Tf 50 700 Td (Picture) Tj ET\n"
                   b"/Artifact BMC q 20 0 0 20 50 500 cm BI /W 1 /H 1 /CS /RGB /BPC 8 ID \xff\x00\x00 EI Q EMC")
        text, images = self.parse(sample_pdf.build_sample_pdf(tagged=False, page_content=content))
        self.assertEqual(text.strip(), "Picture\n\nIMAGE-0")
        self.assertEqual(len(images), 1)
        with Image.open(BytesIO(images[0])) as image:
            self.assertEqual(image.convert("RGB").getpixel((image.width // 2, image.height // 2)), (255, 0, 0))

    def test_nested_form_resolves_image_object_instead_of_colliding_name(self):
        from PIL import Image
        from pypdf import PdfReader, PdfWriter
        from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject, ArrayObject

        writer = PdfWriter(clone_from=PdfReader(BytesIO(sample_pdf.build_sample_pdf(tagged=False))))
        raster = DecodedStreamObject()
        raster.set_data(bytes([0, 0, 255]) * 16)
        raster.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
                       NameObject("/Width"): NumberObject(4), NameObject("/Height"): NumberObject(4),
                       NameObject("/ColorSpace"): NameObject("/DeviceRGB"), NameObject("/BitsPerComponent"): NumberObject(8)})
        form = DecodedStreamObject()
        form.set_data(b"/Im1 Do")
        form.update({NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Form"),
                     NameObject("/BBox"): ArrayObject([NumberObject(n) for n in (0, 0, 1, 1)]),
                     NameObject("/Resources"): DictionaryObject({NameObject("/XObject"): DictionaryObject({
                         NameObject("/Im1"): writer._add_object(raster)})})})
        page = writer.pages[0]
        page["/Resources"]["/XObject"][NameObject("/Fm")] = writer._add_object(form)
        content = DecodedStreamObject()
        content.set_data(b"BT /F1 11 Tf 50 700 Td (Picture) Tj ET\nq 20 0 0 20 50 500 cm /Fm Do Q")
        page[NameObject("/Contents")] = writer._add_object(content)
        output = BytesIO()
        writer.write(output)
        text, images = self.parse(output.getvalue())
        self.assertEqual(text.strip(), "Picture\n\nIMAGE-0")
        self.assertEqual(len(images), 1)  # unused red /Im1 in the page's resources is not a displayed image
        with Image.open(BytesIO(images[0])) as image:
            self.assertEqual(image.size, (4, 4))
            self.assertEqual(image.convert("RGB").getpixel((0, 0)), (0, 0, 255))

    def test_mixed_text_and_scanned_pages_are_refused_before_image_extraction(self):
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        for content in (b"BT /F1 11 Tf 50 700 Td (First page) Tj ET",
                        b"q 300 0 0 500 50 100 cm /Im1 Do Q",
                        b"BT /F1 11 Tf 50 700 Td (Last page) Tj ET"):
            data = sample_pdf.build_sample_pdf(tagged=False, page_content=content)
            writer.add_page(PdfReader(BytesIO(data)).pages[0])
        output = BytesIO()
        writer.write(output)
        images = []
        with self.assertRaises(ParseRefused) as caught:
            PARSERS["pdf"].parse(output.getvalue(), images.append)
        self.assertEqual(caught.exception.code, "unsupported_format")
        self.assertEqual(images, [])

    def test_blank_pages_do_not_make_a_text_pdf_unsupported(self):
        from pypdf import PdfReader, PdfWriter

        writer = PdfWriter()
        writer.add_blank_page(width=600, height=800)
        writer.add_page(PdfReader(BytesIO(sample_pdf.build_sample_pdf(tagged=False))).pages[0])
        output = BytesIO()
        writer.write(output)
        text, images = self.parse(output.getvalue())
        self.assertIn("Getting started", text)
        self.assertEqual(len(images), 1)

