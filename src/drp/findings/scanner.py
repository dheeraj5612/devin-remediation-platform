"""Static scanner for weak / false-green pytest tests.

Heuristics (all AST based, no execution):

* ``no_assertion``      - a test function that never asserts anything (``assert``,
                          ``pytest.raises``, ``mock.assert_*`` ...). These are the classic
                          "happy path, should not raise" tests: they only fail on exceptions.
* ``assert_true``       - a literal ``assert True``.
* ``swallowed_exception`` - ``try: ... except ...: pass/return`` inside a test body, which hides
                          failures of the code under test.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Iterable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

ASSERT_CALL_NAMES = frozenset(
    {
        "raises",
        "warns",
        "deprecated_call",
        "fail",
        "assert_called",
        "assert_called_once",
        "assert_called_with",
        "assert_called_once_with",
        "assert_not_called",
        "assert_any_call",
        "assert_has_calls",
        "assert_frame_equal",
        "assert_series_equal",
        "assert_array_equal",
        "assert_allclose",
    }
)


@dataclass(frozen=True)
class ScanHit:
    file: str
    line: int
    test_name: str
    kind: str
    detail: str = ""


def _call_name(node: ast.Call) -> str:
    func = node.func
    if isinstance(func, ast.Attribute):
        return func.attr
    if isinstance(func, ast.Name):
        return func.id
    return ""


def has_assertion(node: ast.AST) -> bool:
    for child in ast.walk(node):
        if isinstance(child, ast.Assert):
            return True
        if isinstance(child, ast.Call):
            name = _call_name(child)
            if name.startswith("assert") or name in ASSERT_CALL_NAMES:
                return True
    return False


def _is_test_function(node: ast.AST) -> bool:
    return isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith("test")


def _swallowed_handlers(node: ast.AST) -> Iterator[ast.ExceptHandler]:
    for child in ast.walk(node):
        if isinstance(child, ast.Try):
            for handler in child.handlers:
                body = handler.body
                if len(body) == 1 and isinstance(body[0], ast.Pass | ast.Return):
                    yield handler


def scan_source(source: str, path: str) -> list[ScanHit]:
    hits: list[ScanHit] = []
    try:
        tree = ast.parse(source, filename=path)
    except SyntaxError as exc:
        return [ScanHit(path, exc.lineno or 0, "<module>", "syntax_error", str(exc))]
    for node in ast.walk(tree):
        if not _is_test_function(node):
            continue
        assert isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        if not has_assertion(node):
            hits.append(ScanHit(path, node.lineno, node.name, "no_assertion"))
        for stmt in ast.walk(node):
            if (
                isinstance(stmt, ast.Assert)
                and isinstance(stmt.test, ast.Constant)
                and stmt.test.value is True
            ):
                hits.append(ScanHit(path, stmt.lineno, node.name, "assert_true"))
        for handler in _swallowed_handlers(node):
            hits.append(
                ScanHit(path, handler.lineno, node.name, "swallowed_exception", "except: pass")
            )
    return hits


def iter_test_files(root: Path) -> Iterator[Path]:
    for path in sorted(root.rglob("*.py")):
        name = path.name
        if name.startswith("test_") or name.endswith("_test.py") or name.endswith("_tests.py"):
            yield path


def scan_paths(root: Path, files: Iterable[Path] | None = None) -> list[ScanHit]:
    hits: list[ScanHit] = []
    for path in files if files is not None else iter_test_files(root):
        rel = str(path.relative_to(root)) if path.is_absolute() else str(path)
        hits.extend(scan_source(path.read_text(encoding="utf-8"), rel))
    return hits


def hits_to_json(hits: list[ScanHit]) -> str:
    return json.dumps([asdict(h) for h in hits], indent=2)
