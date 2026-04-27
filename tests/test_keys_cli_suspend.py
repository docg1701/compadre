"""End-to-end CLI tests for ``compadre-keys suspend`` (Story 3.1).

Lean by design (test-suite review 2026-04-25): one happy path,
one already-suspended / not-found rejection.
"""

from __future__ import annotations

import sqlite3

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import clear_buffers, runner, seed_by_prefix


def test_suspend_active_key_flips_status_and_audits(env: dict[str, str]) -> None:
    """Active → suspended; one audit row with action='suspend_key'."""
    _, _, prefix = seed_by_prefix(env["db_path"], paid_until="2026-04-30")
    result = runner.invoke(app, ["suspend", prefix])
    assert result.exit_code == 0

    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute("SELECT status FROM api_key").fetchone()
        assert row[0] == "suspended"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = 'suspend_key'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_suspend_already_suspended_or_missing_exits_3(env: dict[str, str]) -> None:
    _, _, prefix = seed_by_prefix(
        env["db_path"], status="suspended", paid_until="2026-04-30"
    )
    result = runner.invoke(app, ["suspend", prefix])
    assert result.exit_code != 0
    clear_buffers()

    result = runner.invoke(app, ["suspend", "NOPENOPE"])
    assert result.exit_code == 3
