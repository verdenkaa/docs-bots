"""Полная сверка объявлений Python с документами-владельцами без исполнения примеров."""

import ast
import re
import unittest
from pathlib import Path


APP = Path(__file__).resolve().parents[1]
ROOT = APP.parent
BASE_DTO_OWNER = "docs/documents/faq/dto.md"


def contract_shape(node):
    """Сравнить контракт, сохраняя defaults, ограничения, конфигурацию и валидаторы."""
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return (
            type(node).__name__,
            ast.dump(node.args),
            ast.dump(node.returns) if node.returns is not None else None,
            tuple(ast.dump(item) for item in node.type_params),
        )

    class WithoutProse(ast.NodeTransformer):
        def visit_Expr(self, item):
            if isinstance(item.value, ast.Constant) and isinstance(item.value.value, str):
                return None
            return self.generic_visit(item)

        def visit_AnnAssign(self, item):
            item = self.generic_visit(item)
            value = item.value
            if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "Field":
                if not value.args and not value.keywords:
                    item.value = None
                elif not value.args and len(value.keywords) == 1 and value.keywords[0].arg == "default":
                    item.value = value.keywords[0].value
            return item

        def visit_Call(self, item):
            item = self.generic_visit(item)
            if isinstance(item.func, ast.Name) and item.func.id == "Field":
                item.keywords = [kw for kw in item.keywords if kw.arg != "description"]
            return item

    return ast.dump(WithoutProse().visit(ast.parse(ast.unparse(node))).body[0])


def python_blocks(text, path):
    """Прочитать оба вида ограждений Markdown; не скрывать ошибочный Python-блок."""
    fence = None
    lines = []
    for line_no, line in enumerate(text.splitlines(), 1):
        if fence is None:
            match = re.fullmatch(r"(`{3,}|~{3,})python\s*", line)
            if match:
                fence = match[1]
                start = line_no + 1
        elif re.fullmatch(re.escape(fence[0]) + "{" + str(len(fence)) + r",}\s*", line):
            try:
                yield ast.parse("\n".join(lines)).body
            except SyntaxError as error:
                raise AssertionError(f"{path}:{start}: неверный Python-блок: {error}") from error
            fence = None
            lines = []
        else:
            lines.append(line)
    assert fence is None, f"{path}: незакрытый Python-блок"


def declarations(nodes, model_names, *, documentation=False):
    """Выбрать сигнатуры, модели, технические dataclass и именованные типы."""
    result = {}
    for node in nodes:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and not node.name.startswith("_"):
            name = node.name
        elif isinstance(node, ast.ClassDef):
            # Исключения описываются отдельно: это не DTO с совпадающим телом класса.
            is_exception = any(isinstance(base, ast.Name) and base.id == "Exception" for base in node.bases)
            if not is_exception and (documentation or node.name in model_names):
                name = node.name
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
            if isinstance(target, ast.Name) and target.id[0].isupper():
                name = target.id
        if name is not None:
            assert name not in result, f"повторное определение {name}"
            result[name] = contract_shape(node)
    return result


def assert_documented_contracts(sources, documents):
    """Сверить точные наборы объявлений, группируя код по Contracts в docstring."""
    trees = {path: ast.parse(text) for path, text in sources.items()}
    model_names = {"DTO"}
    classes = [node for tree in trees.values() for node in tree.body if isinstance(node, ast.ClassDef)]
    while True:
        discovered = {
            node.name for node in classes
            if any(isinstance(base, ast.Name) and base.id in model_names for base in node.bases)
            or any(isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name)
                   and decorator.func.id == "dataclass" for decorator in node.decorator_list)
        }
        if discovered <= model_names:
            break
        model_names |= discovered

    expected = {}
    for path, tree in trees.items():
        owner = re.search(r"^Contracts: (docs/documents/[^\s]+\.md)\.", ast.get_docstring(tree) or "")
        if path == "app/rag/dto.py":
            owner_path = BASE_DTO_OWNER
        elif owner:
            owner_path = owner[1]
        else:
            public_async = any(isinstance(node, ast.AsyncFunctionDef) and not node.name.startswith("_")
                               for node in tree.body)
            models = any(isinstance(node, ast.ClassDef) and node.name in model_names for node in tree.body)
            assert not (public_async or models), f"{path}: не указан документ-владелец Contracts"
            continue
        assert owner_path in documents, f"{path}: документ-владелец отсутствует: {owner_path}"
        target = expected.setdefault(owner_path, {})
        for name, shape in declarations(tree.body, model_names).items():
            assert name not in target, f"{owner_path}: неоднозначное определение {name} в коде"
            target[name] = shape

    actual = {}
    for path, text in documents.items():
        for nodes in python_blocks(text, path):
            for name, shape in declarations(nodes, model_names, documentation=True).items():
                target = actual.setdefault(path, {})
                assert name not in target, f"{path}: повторное определение {name} в документации"
                target[name] = shape

    for path in sorted(expected.keys() | actual.keys()):
        code = expected.get(path, {})
        documented = actual.get(path, {})
        missing = sorted(code.keys() - documented.keys())
        unknown = sorted(documented.keys() - code.keys())
        assert not (missing or unknown), f"{path}: нет в документации {missing}; нет в коде {unknown}"
        for name in code:
            assert code[name] == documented[name], f"{path}: различается контракт {name}"


class DocumentedContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.sources = {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((APP / "rag").rglob("*.py"))
        }
        cls.documents = {
            path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8")
            for path in sorted((ROOT / "docs/documents").rglob("*.md"))
            if "drafts" not in path.relative_to(ROOT / "docs/documents").parts
        }

    def test_documented_contracts_match_source(self):
        assert_documented_contracts(self.sources, self.documents)

    def test_detect_signature_drift(self):
        path = "docs/documents/model-gateway/entrypoints.md"
        original = "async def close_models() -> None: ..."
        variants = [
            "async def close_models(unexpected: int) -> None: ...",
            "async def close_models() -> int: ...",
            "def close_models() -> None: ...",
            "async def renamed_close_models() -> None: ...",
            "",
        ]
        for replacement in variants:
            with self.subTest(replacement=replacement), self.assertRaises(AssertionError):
                documents = dict(self.documents)
                documents[path] = documents[path].replace(original, replacement)
                assert_documented_contracts(self.sources, documents)
        for original, replacement in [
            ("*, timeout_s: float", "timeout_s: float"),
            ("*, timeout_s: float", "*, timeout_s: float = 5.0"),
            ("texts: Sequence[str], purpose:", "purpose:"),
        ]:
            with self.subTest(replacement=replacement), self.assertRaises(AssertionError):
                documents = dict(self.documents)
                documents[path] = documents[path].replace(original, replacement)
                assert_documented_contracts(self.sources, documents)

    def test_detect_dto_and_validation_drift(self):
        variants = [
            ("model-gateway/contracts.md", "class ModelBinding(DTO):", "class RenamedBinding(DTO):"),
            ("model-gateway/contracts.md", "profile_id: NonEmptyText", "profile_id: str"),
            ("model-gateway/contracts.md", "endpoint: str | None", "endpoint: str"),
            ("model-gateway/contracts.md", "endpoint: str | None", "endpoint: str | None = None"),
            ("model-gateway/contracts.md", 'mode="after"', 'mode="before"'),
            ("model-gateway/contracts.md", 'Literal["local", "remote"]', 'Literal["local"]'),
            ("ingestion/contracts.md", "@dataclass(frozen=True)", "@dataclass(frozen=False)"),
            ("faq/dto.md", "strict=True", "strict=False"),
            ("faq/dto.md", "Field(gt=0)", "Field(ge=0)"),
            ("rag-core/contracts.md", "discriminator='kind'", "discriminator='other'"),
        ]
        for suffix, original, replacement in variants:
            with self.subTest(suffix=suffix, replacement=replacement), self.assertRaises(AssertionError):
                path = f"docs/documents/{suffix}"
                self.assertIn(original, self.documents[path])
                documents = dict(self.documents)
                documents[path] = documents[path].replace(original, replacement)
                assert_documented_contracts(self.sources, documents)

    def test_detect_missing_source_or_owner(self):
        path = "app/rag/model_gateway/contracts.py"
        for replacement in [None, self.sources[path].replace("class ModelBinding(DTO):", "class RenamedBinding(DTO):"),
                            self.sources[path].replace("Contracts:", "Former contracts:")]:
            with self.subTest(replacement=replacement is None), self.assertRaises(AssertionError):
                sources = dict(self.sources)
                if replacement is None:
                    del sources[path]
                else:
                    sources[path] = replacement
                assert_documented_contracts(sources, self.documents)

    def test_detect_duplicate_and_malformed_documentation(self):
        path = "docs/documents/model-gateway/entrypoints.md"
        for addition in [
            "\n~~~python\nasync def close_models() -> None: ...\n~~~\n",
            "\n~~~python\nasync def broken(\n~~~\n",
            "\n~~~python\nasync def unfinished() -> None: ...\n",
        ]:
            with self.subTest(addition=addition), self.assertRaises(AssertionError):
                documents = dict(self.documents)
                documents[path] += addition
                assert_documented_contracts(self.sources, documents)

    def test_accept_both_markdown_fences_and_field_descriptions(self):
        documents = dict(self.documents)
        path = "docs/documents/model-gateway/contracts.md"
        documents[path] = documents[path].replace("~~~", "```").replace(
            "profile_id: NonEmptyText", 'profile_id: NonEmptyText = Field(description="Пояснение")')
        assert_documented_contracts(self.sources, documents)


if __name__ == "__main__":
    unittest.main()
