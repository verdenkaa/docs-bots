"""Keep persistence statements behind the SQLAlchemy abstraction boundary."""

import ast
from pathlib import Path
import unittest
from unittest.mock import AsyncMock

from rag.runtime.postgresql._db import _execute


class PersistenceBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def test_repository_executor_requires_sqlalchemy_statements(self):
        session = AsyncMock()
        with self.assertRaises(TypeError):
            await _execute(session, "SELECT 1")
        session.execute.assert_not_awaited()

    def test_runtime_queries_do_not_use_textual_sql(self):
        runtime = Path(__file__).resolve().parents[1] / "rag/runtime"
        for path in runtime.rglob("*.py"):
            if "_migrations" in path.parts:
                continue  # Frozen migration history is independent of current ORM models.
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                if isinstance(node.func, ast.Name) and node.func.id in ("_execute", "_one", "_all"):
                    if len(node.args) > 1:
                        with self.subTest(path=path.name, line=node.lineno):
                            self.assertNotIsInstance(node.args[1], (ast.Constant, ast.JoinedStr))
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                    with self.subTest(path=path.name, line=node.lineno):
                        self.assertNotEqual(node.func.id, "text")
