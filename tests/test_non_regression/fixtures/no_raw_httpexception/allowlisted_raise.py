"""Fixture — intentional FR51 violation SUPPRESSED by the allowlist comment.

Consumed by ``test_scan_respects_allowlist_on_both_kinds``. Exercises the
escape hatch: a same-line ``# scan-allow: <reason>`` comment takes a true
violation out of the scan's output. Applies to both ``raise`` and ``import``
forms; this fixture covers both in one file so a single self-test pins the
full allowlist contract.
"""

from __future__ import annotations

from fastapi import (
    HTTPException,  # scan-allow[no-raw-httpexception]: fixture-intentional-for-4.5f
)


def forbid() -> None:
    raise HTTPException(
        status_code=403
    )  # scan-allow[no-raw-httpexception]: fixture-intentional-for-4.5f
