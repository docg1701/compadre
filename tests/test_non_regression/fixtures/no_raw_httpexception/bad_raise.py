"""Fixture — intentional FR51 violation (raw ``raise HTTPException``).

This file is a test fixture consumed by
``test_no_raw_httpexception.py::test_scan_flags_bad_raise``. It is NOT
imported at runtime and is NOT part of the production codebase. A raw
``raise HTTPException(...)`` is the exact regression pattern the scan must
catch — it bypasses the project's CompadreError hierarchy and breaks the
{error: {message, type, hint}} envelope contract (FR51).
"""

from __future__ import annotations

from fastapi import HTTPException


def forbid() -> None:
    raise HTTPException(status_code=403, detail="forbidden")
