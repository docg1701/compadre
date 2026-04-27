"""Fixture — intentional SQL-injection regression (f-string interpolation).

Consumed by ``test_scan_flags_bad_fstring``. A raw f-string
``conn.execute(f"… WHERE id = {x}")`` is the canonical B608 pattern the
scan must catch — operator input inside the SQL text instead of behind a
? placeholder. Not imported at runtime; not part of the production
codebase.
"""

from __future__ import annotations

import sqlite3


def fetch(conn: sqlite3.Connection, x: int) -> list[tuple[object, ...]]:
    # Intentional regression — the scan MUST flag this line.
    return list(conn.execute(f"SELECT * FROM t WHERE id = {x}"))
