"""End-to-end CLI tests for ``compadre-keys suspend-sweep`` (Story 3.3).

Lean by design (test-suite review 2026-04-25): one happy path that
proves the cron-mode sweep flips expired keys to suspended.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from datetime import date

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import buffers, runner, seed_by_prefix


def test_suspend_sweep_flips_expired_keys_to_suspended(
    env: dict[str, str],
    freeze_today: Callable[[date], None],
) -> None:
    """``paid_until < today`` → flips to suspended; one cron_suspend audit row."""
    freeze_today(date(2026, 4, 16))
    _, kid, _ = seed_by_prefix(
        env["db_path"], key_prefix="EXPIRED1", paid_until="2026-04-10"
    )

    result = runner.invoke(app, ["suspend-sweep"])
    _, stderr = buffers()
    assert result.exit_code == 0, stderr

    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute("SELECT status FROM api_key WHERE id = ?", (kid,)).fetchone()
        assert row[0] == "suspended"
        cron_audits = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'cron_suspend'"
        ).fetchone()[0]
        assert cron_audits >= 1
    finally:
        conn.close()


def test_suspend_sweep_dry_run_does_not_mutate(
    env: dict[str, str],
    freeze_today: Callable[[date], None],
) -> None:
    """``--dry-run`` previews without writing."""
    freeze_today(date(2026, 4, 16))
    _, kid, _ = seed_by_prefix(
        env["db_path"], key_prefix="EXPIRED1", paid_until="2026-04-10"
    )
    result = runner.invoke(app, ["suspend-sweep", "--dry-run"])
    assert result.exit_code == 0
    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute("SELECT status FROM api_key WHERE id = ?", (kid,)).fetchone()
        assert row[0] == "active"
    finally:
        conn.close()
