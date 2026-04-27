"""Tests for ``run_suspension_sweep`` (Story 3.3).

Lean by design (test-suite review 2026-04-25): the sweep contract is
"flip expired manual-active rows to suspended, leave everything else
alone, write per-key audits + one summary row, atomically." Per-status
isolation tests (x 5) and per-audit-row variants collapsed into two
integration tests.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from compadre.keys.db import (
    _apply_migrations,
    generate_customer_id,
    generate_key_id,
    get_connection,
    get_last_sweep_timestamp,
    run_suspension_sweep,
)


def _seed_customer(conn: sqlite3.Connection) -> str:
    cid = generate_customer_id()
    conn.execute(
        "INSERT INTO customer (id, name, email, tax_id_kind, "
        "tax_id_encrypted, notes, registered_at, deleted_at) VALUES "
        "(?, 'Helena', 'h@x.com', 'cpf', ?, '', "
        "'2026-04-01T12:00:00Z', NULL)",
        (cid, b"ct"),
    )
    return cid


def _seed_key(
    conn: sqlite3.Connection,
    cid: str,
    *,
    status: str = "active",
    billing_mode: str = "manual",
    paid_until: str | None = None,
    prefix: str = "KEYAAAAA",
) -> str:
    kid = generate_key_id()
    conn.execute(
        "INSERT INTO api_key (id, customer_id, key_hash, key_prefix, "
        "rate_limit_rpm, status, billing_mode, price_monthly, paid_until, "
        "created_at, last_used_at, notes) VALUES "
        "(?, ?, ?, ?, 60, ?, ?, 149.0, ?, '2026-04-01T12:00:00Z', NULL, '')",
        (
            kid,
            cid,
            kid.replace("key_", "").ljust(64, "0"),
            prefix,
            status,
            billing_mode,
            paid_until,
        ),
    )
    return kid


def test_sweep_flips_expired_active_manual_only(tmp_path: Path) -> None:
    """The full status x billing matrix in one test: only the expired
    manual-active row flips; complimentary, suspended, revoked,
    null-paid-until, and not-yet-expired rows are untouched.
    """
    conn = get_connection(tmp_path / "registry.db")
    try:
        _apply_migrations(conn)
        cid = _seed_customer(conn)
        # Should flip:
        expired = _seed_key(conn, cid, paid_until="2026-04-10", prefix="EXPIRED1")
        # Should NOT flip:
        future = _seed_key(conn, cid, paid_until="2099-12-31", prefix="FUTURE01")
        comp = _seed_key(
            conn,
            cid,
            billing_mode="complimentary",
            paid_until=None,
            prefix="COMP0001",
        )
        trial = _seed_key(conn, cid, paid_until=None, prefix="TRIAL001")
        already_susp = _seed_key(
            conn,
            cid,
            status="suspended",
            paid_until="2026-04-01",
            prefix="ALREADY1",
        )
        revoked = _seed_key(
            conn, cid, status="revoked", paid_until="2026-04-01", prefix="REVOK001"
        )
        conn.commit()

        result = run_suspension_sweep(conn, today=date(2026, 4, 16), actor="cron:test")

        assert result.swept_count == 1
        assert expired in result.swept_key_ids

        # Only the expired row flipped.
        statuses = dict(conn.execute("SELECT id, status FROM api_key").fetchall())
        assert statuses[expired] == "suspended"
        assert statuses[future] == "active"
        assert statuses[comp] == "active"
        assert statuses[trial] == "active"
        assert statuses[already_susp] == "suspended"  # unchanged
        assert statuses[revoked] == "revoked"

        # Audit: one suspend_key per swept row + one cron_suspend summary.
        per_key = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'suspend_key'"
        ).fetchone()[0]
        summary = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'cron_suspend'"
        ).fetchone()[0]
        assert per_key == 1
        assert summary == 1
    finally:
        conn.close()


def test_sweep_zero_rows_still_writes_summary_and_get_last_returns_iso(
    tmp_path: Path,
) -> None:
    """Empty sweep still emits the summary row (FR18 — operator must see
    'cron ran today' even when nothing flipped). ``get_last_sweep_timestamp``
    returns the ISO-Z stamp from that row."""
    conn = get_connection(tmp_path / "registry.db")
    try:
        _apply_migrations(conn)
        result = run_suspension_sweep(conn, today=date(2026, 4, 16), actor="cron:test")
        assert result.swept_count == 0
        summary = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'cron_suspend'"
        ).fetchone()[0]
        assert summary == 1

        ts = get_last_sweep_timestamp(conn)
        assert ts is not None
        assert ts.endswith("Z")
    finally:
        conn.close()
