"""Fixture — Review Finding #2 regression guard.

A multi-line ``Compare`` wraps the operator onto a later physical line; the
``# scan-allow:`` comment lives on that later line, NOT on ``node.lineno``.
The scanner must sweep every physical line from ``node.lineno`` through
``node.end_lineno`` to find the comment. The ``# fmt: off`` block is
intentional — ruff-format would otherwise collapse the compare onto a single
line and defeat the fixture's purpose. Consumed by
``test_scan_allowlist_spans_multi_line_compare``.
"""

from __future__ import annotations


def authenticate(token_hash: str, stored_hash: str) -> bool:
    # fmt: off
    return (
        token_hash
        == stored_hash  # scan-allow[timing-safe-compare]: fixture-multiline-allowlist-coverage  # noqa: E501
    )
    # fmt: on
