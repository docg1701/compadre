"""FR48 non-regression — timing-safe key comparison static check (Story 4.5c).

Walks every ``.py`` under ``src/compadre/`` and asserts no ``==`` / ``!=``
comparison binds to an identifier whose terminal name component matches
``hash`` / ``token`` / ``secret`` (case-insensitive). ``hmac.compare_digest``
is a Call node, structurally outside the visitor's scope. Inline
``# scan-allow[timing-safe-compare]: <reason>`` suppresses per line.

``key`` is deliberately excluded from the suspect set: a 2026-04-23 grep
showed 15+ legitimate hits across ``keys/cli/commands/`` / ``billing.py``
that don't compare key MATERIAL. Real key material in this codebase
binds a ``hash`` / ``token`` / ``secret`` identifier (canonical:
``token_hash``, ``key_hash``, ``_DUMMY_HASH``, ``bearer_token``).
"""

from __future__ import annotations

import ast
import re
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.test_non_regression._scan_common import (
    allowlisted_lines,
    safe_parse,
)

_PROJECT_ROOT: Path = Path(__file__).resolve().parents[2]
_SRC_ROOT: Path = _PROJECT_ROOT / "src" / "compadre"
_FIXTURES_ROOT: Path = (
    Path(__file__).resolve().parent / "fixtures" / "timing_safe_compare"
)
_SCAN_LABEL: str = "timing-safe-compare"
_SUSPECT_IDENTIFIER_RE: re.Pattern[str] = re.compile(r"(?i)(hash|token|secret)")


@dataclass(frozen=True)
class _Violation:
    path: Path
    lineno: int
    identifier: str
    source: str

    def render(self) -> str:
        rel = self.path.relative_to(_PROJECT_ROOT)
        return (
            f"{rel}:{self.lineno}: bare equality compare on "
            f"'{self.identifier}' — {self.source.strip()}"
        )


def _terminal_identifier(node: ast.expr) -> str | None:
    """Rightmost identifier of an operand: ``Name.id``, ``Attribute.attr``,
    or recurse into ``Subscript.value``. Returns ``None`` otherwise."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Subscript):
        return _terminal_identifier(node.value)
    return None


def _find_unsafe_comparisons(path: Path) -> list[_Violation]:
    """AST-walk ``path`` for ``Compare`` nodes whose Eq/NotEq operands have
    a suspect terminal identifier; ``# scan-allow:`` on any spanned line
    suppresses (tokenize-based, so string-literal occurrences don't bypass).
    """
    source = path.read_text(encoding="utf-8")
    source_lines = source.splitlines()
    tree = safe_parse(path)
    allowlisted = allowlisted_lines(source, label=_SCAN_LABEL)
    violations: list[_Violation] = []

    for node in ast.walk(tree):
        if not isinstance(node, ast.Compare):
            continue
        # ``any`` (not ``all``) — a chain like ``token_hash == x < y`` mixes
        # Eq with ordering ops and must still flag the Eq operand.
        if not any(isinstance(op, (ast.Eq, ast.NotEq)) for op in node.ops):
            continue

        operands = [node.left, *node.comparators]
        suspect_operands: list[ast.expr] = []
        seen: set[int] = set()
        for i, op in enumerate(node.ops):
            if not isinstance(op, (ast.Eq, ast.NotEq)):
                continue
            for idx in (i, i + 1):
                operand = operands[idx]
                if id(operand) in seen:
                    continue
                seen.add(id(operand))
                suspect_operands.append(operand)

        start = node.lineno
        end = node.end_lineno or node.lineno
        if any(lineno in allowlisted for lineno in range(start, end + 1)):
            continue

        for operand in suspect_operands:
            ident = _terminal_identifier(operand)
            if ident is None or not _SUSPECT_IDENTIFIER_RE.search(ident):
                continue
            line_index = node.lineno - 1
            line = (
                source_lines[line_index] if 0 <= line_index < len(source_lines) else ""
            )
            violations.append(
                _Violation(path=path, lineno=node.lineno, identifier=ident, source=line)
            )
            # One violation per Compare node — enumerating each operand is noise.
            break

    return violations


@pytest.mark.non_regression
def test_src_compadre_has_no_unsafe_key_comparisons() -> None:
    """FR48 / NFR11: bare ``==`` / ``!=`` on key material is prohibited in
    ``src/compadre/`` outside ``# scan-allow:``-annotated lines."""
    paths = sorted(_SRC_ROOT.rglob("*.py"))
    assert paths, f"No .py files under {_SRC_ROOT}"
    all_violations: list[_Violation] = []
    for path in paths:
        all_violations.extend(_find_unsafe_comparisons(path))

    assert not all_violations, (
        "Bare equality compare on key material in src/compadre/.\n"
        "Use ``hmac.compare_digest(a, b)`` instead of ``a == b`` when either\n"
        "operand holds hashed/tokenised key material. To intentionally bypass:\n"
        "    # scan-allow[timing-safe-compare]: <why this is safe>\n\n"
        "Violations:\n  " + "\n  ".join(v.render() for v in all_violations)
    )


@pytest.mark.non_regression
def test_scan_self_tests_pass_all_fixture_cases() -> None:
    """One self-test covers all 5 scanner contracts via the fixtures dir:
    flagging, allowlist, compare_digest skip, string-literal-bypass guard,
    chained-mixed-ops, multi-line allowlist span. Per-contract isolation
    was 5 separate functions — collapsed since the fixtures themselves
    document each contract one-to-one."""

    def _ids(path_name: str) -> list[str]:
        return [
            v.identifier for v in _find_unsafe_comparisons(_FIXTURES_ROOT / path_name)
        ]

    # bad_example.py: flags one ``token_hash == stored`` violation.
    assert _ids("bad_example.py") == ["token_hash"]

    # allowlisted_example.py: ``# scan-allow:`` suppresses → zero violations.
    assert _ids("allowlisted_example.py") == []

    # compare_digest_only.py: Call node, not Compare → zero violations.
    assert _ids("compare_digest_only.py") == []

    # string_literal_allowlist_bypass.py: tokenize distinguishes STRING
    # tokens from COMMENT — bypass attempt MUST still flag.
    assert _ids("string_literal_allowlist_bypass.py") == ["token_hash"]

    # chained_mixed_ops_example.py: ``any``-not-``all`` operator gating.
    assert _ids("chained_mixed_ops_example.py") == ["token_hash"]

    # multiline_allowlist_example.py: allow on any spanned line suppresses.
    assert _ids("multiline_allowlist_example.py") == []
