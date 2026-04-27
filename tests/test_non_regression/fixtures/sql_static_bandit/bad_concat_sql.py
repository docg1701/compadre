"""Fixture — intentional SQL-injection regression (%-formatting / concat).

Consumed by ``test_scan_flags_bad_concat``. ``"… '" + str(x) + "'"`` and
``"… %s" % x`` both belong to the same risk class as the f-string form —
operator input substituted into the SQL TEXT. Bandit's B608 flags all
three shapes uniformly. This fixture uses the %-formatting variant to
pin that the scan doesn't only catch f-strings.
"""

from __future__ import annotations

import sqlite3


def fetch(conn: sqlite3.Connection, x: int) -> list[tuple[object, ...]]:
    # Intentional regression — the scan MUST flag this line.
    return list(conn.execute("SELECT * FROM t WHERE id = %s" % x))  # noqa: UP031
