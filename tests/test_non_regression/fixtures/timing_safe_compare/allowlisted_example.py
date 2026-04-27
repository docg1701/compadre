"""Fixture — intentional FR48 violation SUPPRESSED by the allowlist comment.

Consumed by ``test_scan_respects_allowlist_comment``. Exercises the escape
hatch: a same-line ``# scan-allow[timing-safe-compare]: <reason>`` comment
takes a true violation out of the scan's output. Reviewer-enforced at PR
time.
"""

from __future__ import annotations


def authenticate(t_hash: str, s_hash: str) -> bool:
    return t_hash == s_hash  # scan-allow[timing-safe-compare]: fixture
