"""Fixture — an intentional B608 site SUPPRESSED via ``# nosec B608``.

Consumed by ``test_scan_respects_nosec``. Demonstrates bandit's native
escape hatch for reviewer-approved exceptions: the f-string SPLICES ONLY
a hardcoded column-allowlist fragment (``set_parts`` — mimics the real
pattern at ``src/compadre/keys/db.py``), and VALUES bind via the
positional ? params. A trailing ``# nosec B608  # <reason>`` comment
suppresses the finding (two-comment form: bandit reads only test-ids
after ``# nosec`` until the next ``#``, so the reason on the second
comment never reaches bandit's tolerant nosec lexer and does not
generate "Test in comment" stderr noise). Scan returns zero findings.
"""

from __future__ import annotations

import sqlite3


def update(conn: sqlite3.Connection, email: str, customer_id: str) -> None:
    # ``set_parts`` is a hardcoded column-allowlist — identical to the
    # production pattern in src/compadre/keys/db.py. VALUES bind via ?.
    set_parts = ["email = ?"]
    params: list[object] = [email, customer_id]
    sql = (
        f"UPDATE customer SET {', '.join(set_parts)} "  # nosec B608  # fixture mirrors the production column-allowlist pattern; only ? placeholders bind values
        "WHERE id = ?"
    )
    conn.execute(sql, params)
