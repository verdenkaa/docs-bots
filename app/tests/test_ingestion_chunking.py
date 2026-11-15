"""Real splitting preserves placeholders, headings and complete Markdown blocks."""

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
