"""Fixture — intentional FR48 violation for self-test coverage.

This file is a test fixture consumed by
``test_timing_safe_compare.py::test_scan_flags_bad_example``. It is NOT
imported at runtime and is NOT part of the production codebase. A bare
equality compare on ``token_hash`` is the exact regression pattern the scan
must catch.
"""

from __future__ import annotations


def authenticate(token_hash: str, stored_hash: str) -> bool:
    # This line is intentionally unsafe — the scan MUST flag it.
    return token_hash == stored_hash
