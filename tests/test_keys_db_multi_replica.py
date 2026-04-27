"""Story 4.4 — multi-replica safety regression for ``get_connection`` (FR44, NFR29).

Compadre 2.0 lets operators scale ``deploy.replicas`` in ``compose.yaml``.
Every replica opens its own SQLite connection against the same registry
file under WAL mode. These tests pin the contract: WAL on every fresh
connection, mode 0700/0600 enforced, and concurrent reader during open
writer transaction (the load-bearing multi-replica property).
"""

from __future__ import annotations

import sqlite3
import stat
from pathlib import Path

import pytest

from compadre.keys.db import (
    _apply_migrations,
    generate_customer_id,
    get_connection,
)

_NOW = "2026-04-20T12:00:00Z"
_INSERT_CUSTOMER = (
    "INSERT INTO customer ("
    "id, name, email, tax_id_kind, tax_id_encrypted, notes, "
    "registered_at, deleted_at"
    ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)"
)


def test_two_connections_concurrent_read_during_open_writer_txn(
    tmp_path: Path,
) -> None:
    """FR44 + NFR29: WAL lets a reader proceed while a writer's tx is
    still open. Replica A holds an uncommitted BEGIN IMMEDIATE; replica
    B reads the committed snapshot without ``SQLITE_BUSY`` and does NOT
    see A's in-flight write."""
    db_path = tmp_path / "registry.db"
    conn_a = get_connection(db_path)
    _apply_migrations(conn_a)
    conn_b = get_connection(db_path)
    try:
        cid_pre = generate_customer_id()
        with conn_a:
            conn_a.execute(
                _INSERT_CUSTOMER,
                (cid_pre, "Pre", "pre@x.com", "cpf", b"\x00", "", _NOW, None),
            )

        # A opens an explicit IMMEDIATE tx and inserts without committing.
        conn_a.execute("BEGIN IMMEDIATE")
        cid_mid = generate_customer_id()
        conn_a.execute(
            _INSERT_CUSTOMER,
            (cid_mid, "Mid", "mid@x.com", "cpf", b"\x00", "", _NOW, None),
        )

        # B reads while A holds the writer lock.
        # WAL: no SQLITE_BUSY; returns the pre-A-commit snapshot.
        pre = conn_b.execute(
            "SELECT name FROM customer WHERE id = ?", (cid_pre,)
        ).fetchall()
        assert [r["name"] for r in pre] == ["Pre"]
        mid = conn_b.execute(
            "SELECT name FROM customer WHERE id = ?", (cid_mid,)
        ).fetchall()
        assert mid == []  # A's write is uncommitted — invisible to B.

        # B trying to write is blocked: WAL serialises writers.
        with pytest.raises(sqlite3.OperationalError, match="database is locked"):
            conn_b.execute("BEGIN IMMEDIATE")

        # A commits; B sees the new row on its next read (new snapshot).
        conn_a.execute("COMMIT")
        conn_b.commit()  # end any implicit read tx so SELECT opens fresh
        post = conn_b.execute(
            "SELECT name FROM customer WHERE id = ?", (cid_mid,)
        ).fetchall()
        assert [r["name"] for r in post] == ["Mid"]

        assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
    finally:
        conn_a.close()
        conn_b.close()


def test_journal_mode_is_wal_on_every_fresh_connection_and_modes_enforced(
    tmp_path: Path,
) -> None:
    """Every fresh ``get_connection`` independently sets WAL mode at open;
    the parent dir lands on 0700 and the DB file on 0600 (NFR13).

    Unique DB path per iteration — reusing one path would be tautological
    (WAL is a persistent file-level attribute)."""
    # Three fresh DBs, three independent WAL pragmas.
    for i in range(3):
        db_path = tmp_path / f"registry-{i}.db"
        conn = get_connection(db_path)
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            assert mode.lower() == "wal"
            assert stat.S_IMODE(db_path.stat().st_mode) == 0o600
        finally:
            conn.close()

    # FR42 + NFR13: parent dir mode also enforced.
    db_path = tmp_path / "subdir" / "registry.db"
    assert not db_path.parent.exists()
    conn = get_connection(db_path)
    try:
        assert stat.S_IMODE(db_path.parent.stat().st_mode) == 0o700
    finally:
        conn.close()
