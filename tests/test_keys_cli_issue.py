"""End-to-end CLI tests for ``compadre-keys issue-key`` (Story 1.2).

Lean by design (test-suite review 2026-04-25): one happy path per
billing mode, one error envelope test, one customer-not-found.
Exhaustive boundary spam (--rpm 0, -1, > cap, NaN, negative price)
deleted; Click + pydantic test their own validation.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import date

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import (
    buffers,
    create_customer_via_cli,
    runner,
)


def test_issue_key_manual_happy_path(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Issue manual key → plaintext printed once + DB row + audit row.

    Asserts every contract a downstream operator depends on: plaintext
    line shape, copy warning, DB row business fields, audit details
    contain only metadata (no PII)."""
    freeze_today(date(2026, 4, 13))
    customer_id = create_customer_via_cli()

    result = runner.invoke(
        app,
        ["issue-key", "--customer", customer_id, "--rpm", "60", "--price", "149"],
    )
    stdout, stderr = buffers()
    assert result.exit_code == 0, stderr
    assert stderr == ""
    match = re.search(r"cmpd_live_[A-Za-z0-9_-]{43}", stdout)
    assert match is not None
    plaintext = match.group(0)
    assert stdout.count(plaintext) == 1
    assert "copy now" in stdout
    assert "60/min" in stdout
    assert "R$ 149,00/month" in stdout

    conn = sqlite3.connect(env["db_path"])
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM api_key").fetchone()
        assert row["customer_id"] == customer_id
        assert row["rate_limit_rpm"] == 60
        assert row["billing_mode"] == "manual"
        assert row["price_monthly"] == 149.0
        assert row["status"] == "active"
        assert re.fullmatch(r"[0-9a-f]{64}", row["key_hash"])
        assert row["key_hash"] not in stdout

        audit = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'issue_key'"
        ).fetchone()
        assert json.loads(audit[0]) == {"billing_mode": "manual", "rate_limit_rpm": 60}
    finally:
        conn.close()


def test_issue_key_complimentary_happy_path(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Complimentary mode stores price=0 and skips first-charge calc."""
    freeze_today(date(2026, 4, 13))
    customer_id = create_customer_via_cli()

    result = runner.invoke(
        app,
        [
            "issue-key",
            "--customer",
            customer_id,
            "--rpm",
            "120",
            "--billing-mode",
            "complimentary",
        ],
    )
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "complimentary" in stdout

    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute("SELECT billing_mode, price_monthly FROM api_key").fetchone()
        assert row[0] == "complimentary"
        assert row[1] == 0.0
    finally:
        conn.close()


def test_issue_key_customer_not_found_exits_3(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Missing customer → exit 3, no key written, no audit row."""
    freeze_today(date(2026, 4, 13))
    result = runner.invoke(
        app,
        [
            "issue-key",
            "--customer",
            "cust_missingxxxxxxxxxxxxxxxx",
            "--rpm",
            "60",
            "--price",
            "149",
        ],
    )
    _, stderr = buffers()
    assert result.exit_code == 3
    assert "Customer not found" in stderr

    conn = sqlite3.connect(env["db_path"])
    try:
        assert conn.execute("SELECT COUNT(*) FROM api_key").fetchone()[0] == 0
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = 'issue_key'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_issue_key_rejects_invalid_inputs(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Mode/price/rpm invariants exit 2: complimentary+price, manual
    no-price, --rpm out of range. Collapsed from 9 boundary tests."""
    freeze_today(date(2026, 4, 13))
    customer_id = create_customer_via_cli()

    for bad_args in (
        ["issue-key", "--customer", customer_id, "--rpm", "60"],  # manual no --price
        [
            "issue-key",
            "--customer",
            customer_id,
            "--rpm",
            "60",
            "--billing-mode",
            "complimentary",
            "--price",
            "10",
        ],
        ["issue-key", "--customer", customer_id, "--rpm", "10001", "--price", "149"],
    ):
        result = runner.invoke(app, bad_args)
        assert result.exit_code == 2, bad_args
