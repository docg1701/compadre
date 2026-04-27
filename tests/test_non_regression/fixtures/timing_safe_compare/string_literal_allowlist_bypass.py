"""Fixture — Review Finding #1 regression guard.

The literal string ``"# scan-allow:"`` appears on the same physical line as a
bare ``==`` on ``token_hash``. A raw-substring allowlist check would
incorrectly suppress the violation; the tokenize-based allowlist correctly
sees a STRING token (not a COMMENT token) and does NOT suppress. Consumed by
``test_scan_rejects_string_literal_allowlist_bypass``.
"""

from __future__ import annotations


def authenticate(token_hash: str, stored_hash: str) -> bool:
    return (_msg := "# scan-allow:") and token_hash == stored_hash and bool(_msg)
