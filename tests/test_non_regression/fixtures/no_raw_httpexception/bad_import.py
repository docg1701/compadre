"""Fixture — intentional FR51 violation (bare ``HTTPException`` import).

Consumed by ``test_scan_flags_bad_import``. Imports ``HTTPException``
without using it — the import ALONE is a risk surface (a subsequent diff
could add ``raise HTTPException(...)`` referencing this name, and the
reviewer might miss it if the name is already in scope). The scan flags
the import even when the class is unused in this module. The trailing
``# noqa: F401`` silences ruff's unused-import check but does NOT silence
4.5f's scan (different token, different line-comment semantics).
"""

from __future__ import annotations

from fastapi.exceptions import HTTPException  # noqa: F401


def noop() -> None:
    pass
