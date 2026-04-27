"""Fixture — the canonical safe pattern.

Consumed by ``test_scan_ignores_compare_digest_calls``. ``hmac.compare_digest``
is a ``Call`` node, not a ``Compare`` node, so the scan's visitor structurally
skips it. This fixture exists to pin that structural property in an explicit
test rather than rely on it being self-evident from the scan's implementation.
"""

from __future__ import annotations

import hmac


def authenticate(token_hash: str, stored_hash: str) -> bool:
    return hmac.compare_digest(token_hash, stored_hash)
