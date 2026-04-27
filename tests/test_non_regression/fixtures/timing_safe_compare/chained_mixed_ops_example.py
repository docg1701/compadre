"""Fixture — Review Finding #3 regression guard.

A chained comparison mixing ``==`` with ``<`` — ``token_hash == x < y`` —
must be flagged. The operator-gating quantifier is ``any`` (not ``all``): any
Eq/NotEq operator in the chain causes the adjacent operands to be checked.
Consumed by ``test_scan_flags_chained_mixed_ops``.
"""

from __future__ import annotations


def authenticate(token_hash: str, x: str, y: str) -> bool:
    return token_hash == x < y
