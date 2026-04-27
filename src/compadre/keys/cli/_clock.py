"""Thin clock seam — tests freeze via monkeypatch.

Lives in its own module so both ``customers.py`` (show) and ``keys.py``
(issue-key, list-keys) can import without creating a lateral dep.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo


def resolve_today_in_tz(tz: ZoneInfo) -> date:
    """Return today's date resolved in ``tz``. Thin seam for tests."""
    return datetime.now(tz).date()
