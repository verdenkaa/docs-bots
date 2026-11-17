"""Real splitting preserves placeholders, headings and complete Markdown blocks."""

import importlib.util
from pathlib import Path
import re
import tempfile
import unittest
from unittest.mock import patch
from uuid import uuid4

from rag.contracts import ImageRef, StepError, StorageError
from rag.ingestion.contracts import StagedUpload, UploadSource
from rag.ingestion.worker import prepare_chunks


class ChunkingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "converted.md"
        self.env = patch.dict("os.environ", {
            "INGESTION_CHUNK_CHARACTERS": "120", "INGESTION_CHUNK_OVERLAP": "20",
            "INGESTION_EMBEDDING_INPUT_CHARACTERS": "120",
        })
        self.env.start()
        self.addCleanup(self.env.stop)

    async def split(self, text, images=()):
        self.path.write_text(text, encoding="utf-8", newline="\n")
        return await prepare_chunks(StagedUpload(path=self.path,
            source=UploadSource(filename="guide.md", access_level="staff"), images=list(images)))

    async def test_sections_and_long_paragraph_keep_all_words(self):
        text = "# First\n\n" + " ".join(f"word{i}" for i in range(50)) + "\n\n# Second\n\nEnd"
        chunks = await self.split(text)
        self.assertTrue(all(len(chunk.text) <= 120 for chunk in chunks))
        words = [word for chunk in chunks for word in re.findall(r'\bword\d+\b', chunk.text)]
        self.assertEqual(list(dict.fromkeys(words)), [f"word{i}" for i in range(50)])
        self.assertEqual(chunks[-1].locator.section, "Second")
        self.assertNotIn("word49", chunks[-1].text)

    async def test_image_attached_to_text_is_indivisible_and_keeps_description(self):
        image = ImageRef(image_id=uuid4(), description="A B")
        marker = f"![A B](image://{image.image_id})"
        chunks = await self.split("word " * 18 + "prefix" + marker + "suffix " * 10, [image])
        self.assertTrue(any(marker in chunk.text for chunk in chunks))
        for chunk in chunks:
            self.assertEqual(chunk.images, [image] if marker in chunk.text else [])

    async def test_unknown_or_lost_images_are_invalid(self):
        with self.assertRaises(StorageError):
            await self.split(f"![Unknown](image://{uuid4()})")

    async def test_table_and_code_over_budget_are_refused_without_truncation(self):
        for text in ("| A | B |\n|---|---|\n" + "| 1 | 2 |\n" * 20,
                     "```python\n" + "x = 1\n" * 30 + "```\n"):
            with self.assertRaisesRegex(StepError, "chunk_too_large"):
                await self.split(text)

    async def test_small_code_block_does_not_create_fake_section(self):
        chunks = await self.split("# Real\n\n```md\n# Not a heading\n```\n\nAfter")
        self.assertEqual([chunk.locator.section for chunk in chunks], ["Real"])
        self.assertIn("# Not a heading", chunks[0].text)

    async def test_small_table_and_code_are_preserved_as_complete_blocks(self):
        for block in ('| A | B |\n|---|---|\n| 1 | 2 |\n',
                      '```python\nx = 1\nprint(x)\n```\n'):
            with self.subTest(block=block):
                chunks = await self.split('Before ' * 10 + '\n\n' + block + '\nAfter')
                self.assertTrue(any(block.strip() in chunk.text for chunk in chunks))

    async def test_overlap_and_determinism(self):
        text = " ".join(f"item{i}" for i in range(70))
        chunks = await self.split(text)
        self.assertEqual(chunks, await self.split(text))
        self.assertTrue(any(word in chunks[1].text.split() for word in chunks[0].text.split()[-2:]))

    async def test_budget_cannot_exceed_embedding_limit(self):
        with patch.dict("os.environ", {"INGESTION_EMBEDDING_INPUT_CHARACTERS": "100"}):
            with self.assertRaisesRegex(StepError, "ingestion_configuration_error"):
                await self.split("Document")

    async def test_section_that_fits_keeps_its_nested_headings(self):
        first = "# First\n\nIntro\n\n## Child\n\n### Detail\n\nBody"
        chunks = await self.split(first + "\n\n# Second\n\nEnd")
        self.assertEqual([chunk.text for chunk in chunks], [first, "# Second\n\nEnd"])
        self.assertEqual([chunk.locator.section for chunk in chunks], ["First", "Second"])

    async def test_oversized_section_descends_only_as_far_as_needed(self):
        first = "## Child A\n\n### Detail A\n\n" + "alpha " * 10
        second = "## Child B\n\n### Detail B\n\n" + "bravo " * 10
        chunks = await self.split("# Parent\n\n" + first + "\n\n" + second)
        self.assertEqual([chunk.text for chunk in chunks],
                         [("# Parent\n\n" + first).strip(), second.strip()])
        self.assertEqual([chunk.locator.section for chunk in chunks], ["Child A", "Child B"])

    async def test_oversized_subsection_descends_to_skipped_heading_level(self):
        first = "#### Detail A\n\n" + "alpha " * 12
        second = "#### Detail B\n\n" + "bravo " * 12
        chunks = await self.split("# Parent\n\n## Child\n\n" + first + "\n\n" + second)
        self.assertEqual([chunk.text for chunk in chunks],
                         [("# Parent\n\n## Child\n\n" + first).strip(), second.strip()])
        self.assertEqual([chunk.locator.section for chunk in chunks], ["Detail A", "Detail B"])

    async def test_preamble_and_section_introduction_are_not_lost(self):
        intro = "introduction " * 15
        text = "Preamble\n\n# Parent\n\n" + intro + "\n\n## Child\n\nEnd"
        chunks = await self.split(text)
        self.assertEqual(chunks[0].text, "Preamble")
        self.assertIsNone(chunks[0].locator)
        self.assertEqual(chunks[-1].text, "## Child\n\nEnd")
        self.assertEqual(chunks[-1].locator.section, "Child")
        self.assertIn("# Parent", chunks[1].text)
        self.assertTrue(all(len(chunk.text) <= 120 for chunk in chunks))
        self.assertGreaterEqual(sum(chunk.text.count("introduction") for chunk in chunks), 15)

    async def test_fallback_preserves_paragraphs_before_using_sentences(self):
        paragraphs = [" ".join([word] * 8) + "." for word in ("alpha", "bravo", "delta")]
        with patch.dict("os.environ", {"INGESTION_CHUNK_OVERLAP": "0"}):
            chunks = await self.split("\n\n".join(paragraphs))
        self.assertEqual([chunk.text for chunk in chunks],
                         ["\n\n".join(paragraphs[:2]), paragraphs[2]])

    async def test_long_paragraph_prefers_complete_sentences(self):
        sentences = [" ".join([word] * 8) + ending
                     for word, ending in (("alpha", "."), ("bravo", "!"), ("delta", "?"))]
        with patch.dict("os.environ", {"INGESTION_CHUNK_OVERLAP": "0"}):
            chunks = await self.split(" ".join(sentences))
        self.assertEqual([chunk.text for chunk in chunks],
                         [" ".join(sentences[:2]), sentences[2]])

    async def test_long_unmarked_document_falls_back_to_words(self):
        words = [f"word{i}" for i in range(900)]
        with patch.dict("os.environ", {"INGESTION_CHUNK_OVERLAP": "0"}):
            chunks = await self.split(" ".join(words))
        self.assertGreater(len(chunks), 1)
        self.assertEqual([word for chunk in chunks for word in chunk.text.split()], words)
        self.assertTrue(all(len(chunk.text) <= 120 and chunk.locator is None for chunk in chunks))

    @unittest.skipUnless(all(importlib.util.find_spec(name) for name in ("pdfplumber", "pypdf")),
                         "нужны pdfplumber и pypdf")
    async def test_pdf_without_headings_is_converted_and_split_within_budget(self):
        import sample_pdf
        from rag.ingestion._parsers import PdfParser

        words = [f"word{i}" for i in range(900)]
        structure = ("Document", {}, [("P", {}, [0])])
        content = ("/P <</MCID 0>> BDC BT /F1 11 Tf 50 700 Td (" + " ".join(words)
                   + ") Tj ET EMC").encode()
        with patch.object(sample_pdf, "_STRUCTURE", structure):
            data = sample_pdf.build_sample_pdf(page_content=content)
        markdown = PdfParser().parse(data, lambda image: self.fail("Unexpected PDF image"))
        with patch.dict("os.environ", {"INGESTION_CHUNK_OVERLAP": "0"}):
            chunks = await self.split(markdown)
        self.assertGreater(len(chunks), 1)
        self.assertEqual([word for chunk in chunks for word in chunk.text.split()], words)
        self.assertTrue(all(len(chunk.text) <= 120 for chunk in chunks))

    async def test_overlap_never_copies_a_code_or_table_fragment(self):
        blocks = ("```python\nx = 1\nprint(x)\n```\n",
                  "~~~python\nx = 1\nprint(x)\n~~~\n",
                  "| A | B |\n|---|---|\n| 1 | 2 |\n")
        with patch.dict("os.environ", {"INGESTION_CHUNK_CHARACTERS": "60"}):
            for block in blocks:
                with self.subTest(block=block):
                    following = "nextword" * 5
                    chunks = await self.split(block + "\n" + following)
                    self.assertEqual([chunk.text for chunk in chunks], [block.strip(), following])

    async def test_overlap_can_copy_a_whole_protected_block(self):
        blocks = ("```\nx\n```\n", "| A |\n|---|\n| x |\n")
        for block in blocks:
            with self.subTest(block=block):
                chunks = await self.split("before " * 13 + "\n\n" + block + "\n" + "after " * 6)
                self.assertEqual(len(chunks), 2)
                for chunk in chunks:
                    self.assertIn(block.strip(), chunk.text)

    async def test_overlap_stops_at_a_protected_block_but_keeps_following_text(self):
        block = "```python\nx = 1\nprint(x)\n```\n"
        with patch.dict("os.environ", {"INGESTION_CHUNK_CHARACTERS": "60"}):
            chunks = await self.split("Earlier\n\n" + block + "\nTail\n\n" + "nextword" * 5)
        self.assertEqual(chunks[1].text, "Tail\n\n" + "nextword" * 5)
        self.assertNotIn("Earlier", chunks[1].text)

    async def test_whole_block_remains_protected_after_repeated_overlap(self):
        for block in ("```\nx\n```\n", "| A |\n|---|\n| x |\n"):
            with self.subTest(block=block), patch.dict("os.environ", {
                    "INGESTION_CHUNK_CHARACTERS": "60", "INGESTION_CHUNK_OVERLAP": "50"}):
                text = "before " * 5 + "\n\n" + block + "\n" + "\n\n".join(
                    f"after{i}" for i in range(12))
                chunks = await self.split(text)
                self.assertGreater(sum(block.strip() in chunk.text for chunk in chunks), 2)
                for chunk in chunks:
                    self.assertLessEqual(len(chunk.text), 60)
                    present = block.strip() in chunk.text
                    self.assertEqual(chunk.text.count("```"), block.count("```") if present else 0)
                    self.assertEqual(chunk.text.count("|"), block.count("|") if present else 0)

    async def test_repeated_overlap_preserves_protected_boundaries_and_image_refs(self):
        image = ImageRef(image_id=uuid4(), description="a. b! c?")
        marker = f"![{image.description}](image://{image.image_id})"
        code = "```\nx\n```"
        table = "| A |\n|---|\n| x |"
        text = ("before " * 15 + "\n\n" + code + "\n\n" + table + "\n\n" + marker
                + "\n\n" + "after " * 60)
        with patch.dict("os.environ", {"INGESTION_CHUNK_OVERLAP": "90"}):
            chunks = await self.split(text, [image])
        self.assertGreater(len(chunks), 3)
        for chunk in chunks:
            self.assertLessEqual(len(chunk.text), 120)
            self.assertEqual(chunk.text.count("```"), 2 if code in chunk.text else 0)
            self.assertEqual(chunk.text.count("|"), table.count("|") if table in chunk.text else 0)
            self.assertEqual(chunk.images, [image] if marker in chunk.text else [])

    async def test_large_placeholder_and_single_word_are_refused(self):
        image = ImageRef(image_id=uuid4(), description="x" * 120)
        for text, images in ((f"![{image.description}](image://{image.image_id})", [image]),
                             ("x" * 121, [])):
            with self.subTest(text=text), self.assertRaisesRegex(StepError, "chunk_too_large"):
                await self.split(text, images)
