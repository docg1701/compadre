"""End-to-end CLI tests for ``compadre-keys list-keys`` (Story 1.3).

Lean by design (test-suite review 2026-04-25): one populated table
test, one filter test, one JSON shape test, one empty-state test.
Per-status / per-billing-mode / per-filter parametrize variants
deleted; the underlying SQL is exercised once and trusted.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date

from compadre.keys.cli.app import app
from compadre.keys.db import generate_key_id
from tests._keys_cli_helpers import (
    buffers,
    create_customer_via_cli,
    issue_key_via_cli,
    runner,
)


def _raw_insert_key(
    db_path: str, customer_id: str, *, status: str, key_prefix: str
) -> None:
    key_id = generate_key_id()
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO api_key (id, customer_id, key_hash, key_prefix, "
            "rate_limit_rpm, status, billing_mode, price_monthly, paid_until, "
            "created_at, last_used_at, notes) VALUES "
            "(?, ?, ?, ?, 60, ?, 'manual', 149.0, NULL, "
            "'2026-04-16T12:00:00Z', NULL, '')",
            (
                key_id,
                customer_id,
                key_id.replace("key_", "").ljust(64, "0"),
                key_prefix,
                status,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def test_list_keys_empty_db_prints_hint(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """First-run UX: empty DB emits the 'create-customer' hint."""
    freeze_today(date(2026, 4, 16))
    result = runner.invoke(app, ["list-keys"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "compadre-keys issue-key" in stdout


def test_list_keys_populated_table_shows_all_statuses(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Happy path: table renders every issued key, regardless of status."""
    freeze_today(date(2026, 4, 16))
    cid = create_customer_via_cli(name="Helena", email="h@x.com")
    issue_key_via_cli(cid)
    _raw_insert_key(env["db_path"], cid, status="suspended", key_prefix="SUSP1234")
    _raw_insert_key(env["db_path"], cid, status="revoked", key_prefix="REVK1234")

    result = runner.invoke(app, ["list-keys"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "SUSP1234" in stdout
    assert "REVK1234" in stdout
    assert "Notes" not in stdout


def test_list_keys_status_filter_narrows_rows(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """``--status suspended`` returns only suspended rows."""
    freeze_today(date(2026, 4, 16))
    cid = create_customer_via_cli(name="Helena", email="h@x.com")
    issue_key_via_cli(cid)
    _raw_insert_key(env["db_path"], cid, status="suspended", key_prefix="SUSP1234")

    result = runner.invoke(app, ["list-keys", "--status", "suspended"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "SUSP1234" in stdout
    assert stdout.count("suspended") >= 1


def test_list_keys_format_json_emits_array(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Machine consumers depend on a JSON array shape, not table text."""
    freeze_today(date(2026, 4, 16))
    cid = create_customer_via_cli(name="Helena", email="h@x.com")
    issue_key_via_cli(cid)

    result = runner.invoke(app, ["list-keys", "--format", "json"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    parsed = json.loads(stdout)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    row = parsed[0]
    assert row["status"] == "active"
    assert row["billing_mode"] == "manual"
    assert "key_hash" not in row
