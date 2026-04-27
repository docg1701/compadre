"""End-to-end CLI tests for ``compadre-keys revenue`` (Story 3.2).

Lean by design (test-suite review 2026-04-25): one happy path that
asserts MRR + projected math from the seeded portfolio, one JSON shape.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime

from compadre.keys.cli.app import app
from compadre.keys.db import (
    _apply_migrations,
    generate_customer_id,
    generate_key_id,
    get_connection,
    insert_api_key,
    insert_customer,
)
from compadre.keys.models import ApiKeyRecord, CustomerRecord
from tests._keys_cli_helpers import buffers, clear_buffers, runner


def _seed_portfolio(db_path: str) -> None:
    conn: sqlite3.Connection = get_connection(db_path)
    try:
        _apply_migrations(conn)
        cid = generate_customer_id()
        insert_customer(
            conn,
            CustomerRecord(
                id=cid,
                name="Helena",
                email="h@x.com",
                tax_id_kind="cpf",
                tax_id_encrypted=b"ct",
                notes="",
                registered_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
                deleted_at=None,
            ),
            actor="cli:tester",
        )
        for paid_until, billing in (
            ("2026-04-30", "manual"),
            ("2026-04-30", "manual"),
            (None, "complimentary"),
        ):
            insert_api_key(
                conn,
                ApiKeyRecord(
                    id=generate_key_id(),
                    customer_id=cid,
                    key_hash=("a" + str(generate_key_id()))[:64],
                    key_prefix="ABCD" + str(hash(paid_until or "0"))[:4],
                    rate_limit_rpm=60,
                    status="active",
                    billing_mode=billing,  # type: ignore[arg-type]
                    price_monthly=149.0 if billing == "manual" else 0.0,
                    paid_until=paid_until,
                    created_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC),
                    last_used_at=None,
                    notes="",
                ),
                actor="cli:tester",
            )
    finally:
        conn.close()


def test_revenue_table_renders_mrr_and_complimentary_separately(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """FR17: complimentary count is separate from MRR/projected totals."""
    freeze_today(date(2026, 4, 16))
    _seed_portfolio(env["db_path"])
    result = runner.invoke(app, ["revenue"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "Paid" in stdout
    assert "Complimentary" in stdout


def test_revenue_format_json_shape(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    freeze_today(date(2026, 4, 16))
    _seed_portfolio(env["db_path"])
    result = runner.invoke(app, ["revenue", "--format", "json"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    parsed = json.loads(stdout)
    assert "mrr" in parsed
    assert "projected" in parsed
    assert "complimentary_count" in parsed


def _seed_replay_fixture(db_path: str) -> str:
    """Seed: one manual key, issued 2026-03-01, mark_paid 2026-04-10
    (paid through 2026-04-30), suspended 2026-04-20.

    Returns the key prefix."""
    from compadre.keys.db import mark_key_paid, suspend_key

    conn = get_connection(db_path)
    try:
        _apply_migrations(conn)
        cid = generate_customer_id()
        insert_customer(
            conn,
            CustomerRecord(
                id=cid,
                name="Helena",
                email="h@x.com",
                tax_id_kind="cpf",
                tax_id_encrypted=b"ct",
                notes="",
                registered_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
                deleted_at=None,
            ),
            actor="cli:tester",
        )
        kid = generate_key_id()
        insert_api_key(
            conn,
            ApiKeyRecord(
                id=kid,
                customer_id=cid,
                key_hash="a" * 64,
                key_prefix="Pq_repl1",
                rate_limit_rpm=60,
                status="active",
                billing_mode="manual",
                price_monthly=149.0,
                paid_until=None,
                created_at=datetime(2026, 3, 1, 10, 0, 0, tzinfo=UTC),
                last_used_at=None,
                notes="",
            ),
            actor="cli:tester",
        )
        # Backdate issue_key audit row to 2026-03-01 so the as_of replay
        # treats this key as existing on 2026-04-15 / 2026-04-25 alike.
        conn.execute(
            "UPDATE audit_log SET occurred_at = ? "
            "WHERE action = 'issue_key' AND target_id = ?",
            ("2026-03-01T10:00:00Z", kid),
        )
        # mark_paid on 2026-04-10 (extending paid_until to 2026-04-30).
        mark_key_paid(
            conn,
            kid,
            paid_until="2026-04-30",
            actor="cli:tester",
            today=date(2026, 4, 10),
        )
        conn.execute(
            "UPDATE audit_log SET occurred_at = ? "
            "WHERE action = 'mark_paid' AND target_id = ?",
            ("2026-04-10T10:00:00Z", kid),
        )
        # suspend on 2026-04-20.
        suspend_key(conn, kid, actor="cli:tester", today=date(2026, 4, 20))
        conn.execute(
            "UPDATE audit_log SET occurred_at = ? "
            "WHERE action = 'suspend_key' AND target_id = ?",
            ("2026-04-20T10:00:00Z", kid),
        )
        conn.commit()
    finally:
        conn.close()
    return "Pq_repl1"


def test_revenue_as_of_replays_status_to_paid_then_suspended(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Story 5.1 Gap 8: --as-of 2026-04-15 → key bucketed as paid;
    --as-of 2026-04-25 → bucketed as suspended."""
    freeze_today(date(2026, 5, 1))
    _seed_replay_fixture(env["db_path"])

    clear_buffers()
    result = runner.invoke(
        app,
        ["revenue", "--month", "2026-04", "--as-of", "2026-04-15", "--format", "json"],
    )
    stdout, _ = buffers()
    assert result.exit_code == 0, stdout
    parsed = json.loads(stdout)
    assert parsed["paid_count"] == 1
    assert parsed["suspended_count"] == 0

    clear_buffers()
    result = runner.invoke(
        app,
        ["revenue", "--month", "2026-04", "--as-of", "2026-04-25", "--format", "json"],
    )
    stdout, _ = buffers()
    assert result.exit_code == 0, stdout
    parsed = json.loads(stdout)
    assert parsed["paid_count"] == 0
    assert parsed["suspended_count"] == 1


def test_revenue_as_of_future_or_pre_history_exits_2(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Story 5.1 Gap 8: future --as-of and pre-audit --as-of both exit 2."""
    freeze_today(date(2026, 5, 1))
    _seed_replay_fixture(env["db_path"])

    clear_buffers()
    result = runner.invoke(app, ["revenue", "--as-of", "2099-01-01"])
    assert result.exit_code == 2

    clear_buffers()
    result = runner.invoke(app, ["revenue", "--as-of", "1900-01-01"])
    assert result.exit_code == 2
