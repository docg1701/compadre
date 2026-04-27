"""Shared scanner helpers for ``tests/test_non_regression/test_*.py``.

F43 + F44 + F45 + F46 hardening — single source of truth for the
``# scan-allow[<scan>]: <reason>`` allowlist convention used by every
non-regression source-scanner. Each scan opts into its own namespace so
a single comment can never accidentally silence two scans at once.

Public surface:

  * :data:`SCAN_LABELS` — canonical scan-name allowlist; rejected lookups
    raise :class:`ValueError` so a typo is caught at import time.
  * :func:`allowlisted_lines(source, label)` — physical line numbers
    carrying a real ``# scan-allow[<label>]: <reason>`` comment. F44
    enforces a >=3 non-whitespace-char reason; F45 enforces the
    ``[<label>]`` namespacing.
  * :func:`safe_tokenize(source)` and :func:`safe_parse(path)` — F46
    wrappers that turn ``tokenize.TokenError`` / ``SyntaxError`` /
    ``IndentationError`` into named ``pytest.fail`` calls instead of
    cryptic crashes naming no path.
"""

from __future__ import annotations

import ast
import io
import tokenize
from collections.abc import Iterable
from pathlib import Path

import pytest

# Each scan registers its short label here. Adding a new scan = one new
# entry plus one ``allowlisted_lines(source, label="...")`` call site.
SCAN_LABELS: frozenset[str] = frozenset(
    {
        "timing-safe-compare",  # 4.5c
        "no-raw-httpexception",  # 4.5f
        "sql-injection",  # 4.5g
    }
)

# F44 — reason must contain at least this many non-whitespace characters
# after the colon. Stops bare ``# scan-allow[...]:`` from silencing a
# violation; the convention requires a human-readable justification.
_MIN_REASON_CHARS = 3


def _expected_prefix(label: str) -> str:
    if label not in SCAN_LABELS:
        raise ValueError(
            f"Unknown scan label {label!r}. "
            f"Add it to SCAN_LABELS first. Known: {sorted(SCAN_LABELS)}"
        )
    return f"# scan-allow[{label}]:"


def allowlisted_lines(source: str, *, label: str) -> set[int]:
    """Return the physical line numbers carrying an allowlist comment for
    the named scan.

    F45 — only ``# scan-allow[<label>]: <reason>`` matches. Bare
    ``# scan-allow:`` is silently ignored (deliberate; it would silence
    every scan in lockstep). Operators must namespace per scan.

    F44 — the reason segment must carry >=3 non-whitespace characters.
    A bare ``# scan-allow[<label>]:`` is rejected; an empty justification
    is the operational equivalent of "trust me".

    Uses :mod:`tokenize` so the allowlist fires only on genuine COMMENT
    tokens — a string-literal occurrence of the token cannot suppress an
    adjacent comparison.
    """
    expected = _expected_prefix(label)
    out: set[int] = set()
    for tok in safe_tokenize(source):
        if tok.type != tokenize.COMMENT:
            continue
        text = tok.string.strip()
        if not text.startswith(expected):
            continue
        reason = text[len(expected) :].strip()
        if len(reason) < _MIN_REASON_CHARS:
            continue
        out.add(tok.start[0])
    return out


def safe_tokenize(source: str) -> Iterable[tokenize.TokenInfo]:
    """F46 — fail with a named pytest message when ``tokenize`` errors out."""
    try:
        return list(tokenize.generate_tokens(io.StringIO(source).readline))
    except (tokenize.TokenError, IndentationError, SyntaxError) as exc:
        pytest.fail(f"Cannot tokenize source: {exc}")
        raise  # unreachable; pytest.fail raises


def safe_parse(path: Path) -> ast.AST:
    """F46 — wrap :func:`ast.parse` with a named failure path."""
    source = path.read_text(encoding="utf-8")
    try:
        return ast.parse(source, filename=str(path))
    except (SyntaxError, ValueError) as exc:
        pytest.fail(f"Cannot ast.parse {path}: {exc}")
        raise  # unreachable
