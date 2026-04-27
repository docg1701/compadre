"""End-to-end CLI tests for ``compadre-keys update-key`` (Story 1.6).

Lean by design (test-suite review 2026-04-25): one happy path
exercising every flag in one txn, one not-found, one usage-error.
Per-flag isolated tests + boundary spam deleted.
"""

from __future__ import annotations

import json
import sqlite3

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import buffers, clear_buffers, runner, seed_by_prefix


def test_update_key_all_three_flags_in_one_txn(env: dict[str, str]) -> None:
    """--rpm + --price + --notes all land in one txn; audit lists all
    three field names."""
    _, _, prefix = seed_by_prefix(env["db_path"])
    result = runner.invoke(
        app,
        [
            "update-key",
            prefix,
            "--rpm",
            "120",
            "--price",
            "199.5",
            "--notes",
            "upgraded",
        ],
    )
    _, stderr = buffers()
    assert result.exit_code == 0, stderr

    conn = sqlite3.connect(env["db_path"])
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute(
            "SELECT rate_limit_rpm, price_monthly, notes FROM api_key"
        ).fetchone()
        assert row["rate_limit_rpm"] == 120
        assert row["price_monthly"] == 199.5
        assert row["notes"] == "upgraded"

        audit = conn.execute(
            "SELECT details_json FROM audit_log "
            "WHERE action = 'update_key' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        details = json.loads(audit[0])
        assert set(details["fields"]) == {"rpm", "price", "notes"}
    finally:
        conn.close()


def test_update_key_revoked_or_missing_exits_3(env: dict[str, str]) -> None:
    """Revoked keys and unknown prefixes both exit 3."""
    _, _, prefix = seed_by_prefix(env["db_path"], status="revoked")
    result = runner.invoke(app, ["update-key", prefix, "--rpm", "120"])
    _, stderr = buffers()
    assert result.exit_code == 3
    assert "revoked" in stderr.lower() or "not found" in stderr.lower()
    clear_buffers()

    result = runner.invoke(app, ["update-key", "NOPENOPE", "--rpm", "120"])
    assert result.exit_code == 3


def test_update_key_no_op_writes_no_audit(env: dict[str, str]) -> None:
    """B25 — passing the existing rpm/price/notes as the only update target
    must NOT produce an audit row + must render the no-op confirmation."""
    # Default seed: rate_limit_rpm=60, price_monthly=149.0, notes="".
    _, _, prefix = seed_by_prefix(env["db_path"])

    result = runner.invoke(
        app, ["update-key", prefix, "--rpm", "60", "--price", "149", "--notes", ""]
    )
    stdout, _stderr = buffers()
    assert result.exit_code == 0
    assert "No changes" in stdout
    assert "already up to date" in stdout

    conn = sqlite3.connect(env["db_path"])
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'update_key'"
        ).fetchone()[0]
        assert count == 0, f"unexpected update_key audit rows: {count}"
    finally:
        conn.close()


def test_update_key_rejects_invalid_inputs(env: dict[str, str]) -> None:
    """No flags, --rpm out of range, --price negative all exit 2."""
    _, _, prefix = seed_by_prefix(env["db_path"])

    result = runner.invoke(app, ["update-key", prefix])
    assert result.exit_code == 2
    clear_buffers()

    result = runner.invoke(app, ["update-key", prefix, "--rpm", "10001"])
    assert result.exit_code == 2
    clear_buffers()

    result = runner.invoke(app, ["update-key", prefix, "--price", "-1"])
    assert result.exit_code == 2
