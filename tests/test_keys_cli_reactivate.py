"""End-to-end CLI tests for ``compadre-keys reactivate`` (Story 3.1).

Lean by design (test-suite review 2026-04-25).
"""

from __future__ import annotations

import sqlite3

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import clear_buffers, runner, seed_by_prefix


def test_reactivate_suspended_key_flips_status_no_paid_until_change(
    env: dict[str, str],
) -> None:
    """suspended → active. paid_until preserved (Story 3.1 contract:
    reactivate does NOT extend billing)."""
    _, _, prefix = seed_by_prefix(
        env["db_path"], status="suspended", paid_until="2026-03-30"
    )
    result = runner.invoke(app, ["reactivate", prefix])
    assert result.exit_code == 0

    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute("SELECT status, paid_until FROM api_key").fetchone()
        assert row[0] == "active"
        assert row[1] == "2026-03-30"  # unchanged
    finally:
        conn.close()


def test_reactivate_already_active_or_missing_exits_3(env: dict[str, str]) -> None:
    _, _, prefix = seed_by_prefix(env["db_path"], paid_until="2026-03-30")
    result = runner.invoke(app, ["reactivate", prefix])
    assert result.exit_code != 0
    clear_buffers()

    result = runner.invoke(app, ["reactivate", "NOPENOPE"])
    assert result.exit_code == 3
