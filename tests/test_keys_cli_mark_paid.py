"""End-to-end CLI tests for ``compadre-keys mark-paid`` (Story 3.1 / 5.1).

Lean by design (test-suite review 2026-04-25): one happy path that
proves the audit + state contract; one not-found / invalid-state.
Story 5.1 Gaps 3 & 4 add ``--months`` and ``--price-monthly`` coverage.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from datetime import date

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import buffers, clear_buffers, runner, seed_by_prefix


def test_mark_paid_extends_paid_until_and_audits(env: dict[str, str]) -> None:
    """Happy path: paid_until updated; audit row written with the new date."""
    _, _, prefix = seed_by_prefix(env["db_path"])
    result = runner.invoke(app, ["mark-paid", prefix, "--until", "2026-06-05"])
    _, stderr = buffers()
    assert result.exit_code == 0, stderr

    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT paid_until FROM api_key WHERE key_prefix = ?", (prefix,)
        ).fetchone()
        assert row[0] == "2026-06-05"
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = 'mark_paid'"
            ).fetchone()[0]
            == 1
        )
    finally:
        conn.close()


def test_mark_paid_missing_key_exits_3(env: dict[str, str]) -> None:
    """Missing key prefix exits 3."""
    result = runner.invoke(app, ["mark-paid", "NOPENOPE", "--until", "2026-06-05"])
    assert result.exit_code == 3


def test_mark_paid_months_extends_from_paid_until_when_active(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Story 5.1 Gap 3: --months on a key with paid_until in the future
    extends from paid_until (paid_until + N months)."""
    freeze_today(date(2026, 4, 1))
    _, _, prefix = seed_by_prefix(env["db_path"], paid_until="2026-04-15")
    result = runner.invoke(app, ["mark-paid", prefix, "--months", "2"])
    _, stderr = buffers()
    assert result.exit_code == 0, stderr
    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT paid_until FROM api_key WHERE key_prefix = ?", (prefix,)
        ).fetchone()
        audit = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'mark_paid' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "2026-06-15"
    details = json.loads(audit[0])
    assert details.get("mode") == "months"
    assert details.get("months") == 2


def test_mark_paid_months_extends_from_today_when_expired(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Story 5.1 Gap 3: --months on an expired key starts from today."""
    freeze_today(date(2026, 5, 1))
    _, _, prefix = seed_by_prefix(env["db_path"], paid_until="2026-04-01")
    result = runner.invoke(app, ["mark-paid", prefix, "--months", "1"])
    assert result.exit_code == 0
    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT paid_until FROM api_key WHERE key_prefix = ?", (prefix,)
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "2026-06-01"


def test_mark_paid_until_and_months_mutex(env: dict[str, str]) -> None:
    """Story 5.1 Gap 3: providing both --until and --months exits 2."""
    _, _, prefix = seed_by_prefix(env["db_path"])
    result = runner.invoke(
        app, ["mark-paid", prefix, "--until", "2026-06-05", "--months", "2"]
    )
    assert result.exit_code == 2


def test_mark_paid_neither_until_nor_months_exits_2(env: dict[str, str]) -> None:
    """Story 5.1 Gap 3: providing neither --until nor --months exits 2."""
    _, _, prefix = seed_by_prefix(env["db_path"])
    result = runner.invoke(app, ["mark-paid", prefix])
    assert result.exit_code == 2


def test_mark_paid_price_monthly_atomic_with_months(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Story 5.1 Gap 4: --price-monthly is atomic with --months; the audit
    row carries old/new; the price rounds to two decimals (half-even)."""
    freeze_today(date(2026, 5, 5))
    _, _, prefix = seed_by_prefix(env["db_path"])
    result = runner.invoke(
        app,
        ["mark-paid", prefix, "--months", "1", "--price-monthly", "100.999"],
    )
    _, stderr = buffers()
    assert result.exit_code == 0, stderr
    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT paid_until, price_monthly FROM api_key WHERE key_prefix = ?",
            (prefix,),
        ).fetchone()
        audit = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'mark_paid' "
            "ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "2026-06-05"
    # 100.999 → ROUND_HALF_EVEN to 2dp = 101.00
    assert abs(row[1] - 101.00) < 1e-9
    details = json.loads(audit[0])
    assert details.get("price_monthly_changed") is True
    assert abs(details["new"] - 101.00) < 1e-9
    assert abs(details["old"] - 149.0) < 1e-9


def test_mark_paid_price_monthly_invalid_exits_2(env: dict[str, str]) -> None:
    """Story 5.1 Gap 4: negative / oversize / NaN price exits 2."""
    _, _, prefix = seed_by_prefix(env["db_path"])
    for bad in ("-1.0", "100000.00", "nan"):
        clear_buffers()
        result = runner.invoke(
            app,
            ["mark-paid", prefix, "--until", "2026-06-05", "--price-monthly", bad],
        )
        assert result.exit_code == 2, bad
