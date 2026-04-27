"""Fixture — the canonical safe pattern (pure parameterised queries).

Consumed by ``test_scan_ignores_parameterised_sql``. Positional ? bindings
for every value; no f-string, no %-formatting, no concat. Bandit B608
does not fire on this shape. Pins the scan's negative contract.
"""

from __future__ import annotations

import sqlite3


def fetch(conn: sqlite3.Connection, x: int) -> list[tuple[object, ...]]:
    return list(conn.execute("SELECT * FROM t WHERE id = ?", (x,)))


def update(conn: sqlite3.Connection, new_email: str, customer_id: str) -> None:
    conn.execute(
        "UPDATE customer SET email = ? WHERE id = ?",
        (new_email, customer_id),
    )
