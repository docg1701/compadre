"""FR51 non-regression — no raw ``HTTPException`` static check (Story 4.5f).

Walks every ``.py`` under ``src/compadre/`` and asserts:
1. No ``raise <something-named-HTTPException>(...)`` (``ast.Raise`` whose
   ``.exc`` terminal name is ``"HTTPException"``).
2. No ``from {fastapi|fastapi.exceptions|starlette.exceptions} import
   HTTPException``.

``# scan-allow[no-raw-httpexception]: <reason>`` on any spanned line
suppresses. Docstring mentions of the string ``"HTTPException"`` are
``ast.Constant`` and structurally outside both rules.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import pytest

from tests.test_non_regression._scan_common import allowlisted_lines, safe_parse

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_ROOT: Path = _PROJECT_ROOT / "src" / "compadre"
_FIXTURES_ROOT: Path = (
    Path(__file__).resolve().parent / "fixtures" / "no_raw_httpexception"
)
_SCAN_LABEL: str = "no-raw-httpexception"

_HTTPEXCEPTION_NAME: str = "HTTPException"
_IMPORT_SOURCE_MODULES: frozenset[str] = frozenset(
    {"fastapi", "fastapi.exceptions", "starlette.exceptions"}
)


@dataclass(frozen=True)
class _Violation:
    path: Path
    lineno: int
    kind: Literal["raise", "import"]
    source: str

    def render(self) -> str:
        rel = self.path.relative_to(_PROJECT_ROOT)
        return (
            f"{rel}:{self.lineno}: raw HTTPException {self.kind} — "
            f"{self.source.strip()}"
        )


def _terminal_function_name(node: ast.expr) -> str | None:
    """Terminal name of a Raise's ``.exc``: bare Name, Call(Name), or
    Call(Attribute) → rightmost name. Anything else → ``None``."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id
        if isinstance(func, ast.Attribute):
            return func.attr
    return None


def _is_raw_httpexception_raise(node: ast.AST) -> bool:
    if not isinstance(node, ast.Raise) or node.exc is None:
        return False
    return _terminal_function_name(node.exc) == _HTTPEXCEPTION_NAME


def _is_httpexception_import(node: ast.AST) -> bool:
    if not isinstance(node, ast.ImportFrom):
        return False
    if node.module not in _IMPORT_SOURCE_MODULES:
        return False
    return any(alias.name == _HTTPEXCEPTION_NAME for alias in node.names)


def _find_raw_httpexception_usages(path: Path) -> list[_Violation]:
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = safe_parse(path)
    allowlisted = allowlisted_lines(source, label=_SCAN_LABEL)
    violations: list[_Violation] = []

    for node in ast.walk(tree):
        kind: Literal["raise", "import"]
        if _is_raw_httpexception_raise(node):
            kind = "raise"
        elif _is_httpexception_import(node):
            kind = "import"
        else:
            continue

        start = node.lineno
        end = node.end_lineno or node.lineno
        if any(lineno in allowlisted for lineno in range(start, end + 1)):
            continue

        line_index = node.lineno - 1
        line = source_lines[line_index] if 0 <= line_index < len(source_lines) else ""
        violations.append(
            _Violation(path=path, lineno=node.lineno, kind=kind, source=line)
        )

    return violations


@pytest.mark.non_regression
def test_src_compadre_has_no_raw_httpexception() -> None:
    """FR51 / NFR17: raw ``HTTPException`` raises AND imports are prohibited
    in ``src/compadre/`` outside ``# scan-allow:``-annotated lines."""
    paths = sorted(_SRC_ROOT.rglob("*.py"))
    assert paths, f"No .py files under {_SRC_ROOT}"
    all_violations: list[_Violation] = []
    for path in paths:
        all_violations.extend(_find_raw_httpexception_usages(path))

    assert not all_violations, (
        "Raw HTTPException usage detected in src/compadre/.\n"
        "Use a CompadreError subclass from src/compadre/exceptions.py:\n"
        "  401 → AuthenticationError / ApiKey{NotFound,Suspended,Revoked}Error\n"
        "  400 → InvalidRequestError / InvalidTaxIdError\n"
        "  429 → RateLimitError    503 → ServiceUnavailableError\n"
        "  500 → CompadreError / KeyRegistryError\n"
        "To intentionally bypass: # scan-allow[no-raw-httpexception]: <reason>\n\n"
        "Violations:\n  " + "\n  ".join(v.render() for v in all_violations)
    )


@pytest.mark.non_regression
def test_scan_self_tests_pass_all_fixture_cases() -> None:
    """Self-test: scanner flags violations + respects allowlists + ignores
    non-violations across all four fixture files."""
    bad_raise = _find_raw_httpexception_usages(_FIXTURES_ROOT / "bad_raise.py")
    assert sorted(v.kind for v in bad_raise) == ["import", "raise"]

    bad_import = _find_raw_httpexception_usages(_FIXTURES_ROOT / "bad_import.py")
    assert len(bad_import) == 1 and bad_import[0].kind == "import"

    assert _find_raw_httpexception_usages(_FIXTURES_ROOT / "allowlisted_raise.py") == []
    assert (
        _find_raw_httpexception_usages(_FIXTURES_ROOT / "compadre_error_only.py") == []
    )
