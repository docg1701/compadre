"""End-to-end CLI tests for ``compadre-keys show`` (Story 1.4).

Lean by design (test-suite review 2026-04-25): kept only the
behavioural contracts a downstream user depends on — full panel labels
on a happy show, LGPD ciphertext non-leak, audit-tail flag, JSON shape,
and the not-found error envelope.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable
from datetime import UTC, date, datetime

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import (
    buffers,
    clear_buffers,
    create_customer_via_cli,
    issue_key_via_cli,
    latest_key_id_and_prefix,
    runner,
)


def test_show_key_renders_full_detail_panel(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """Happy path: every contractual label + business value present, no audit
    section unless ``--audit-tail`` requested."""
    freeze_today(datetime.now(UTC).date())
    customer_id = create_customer_via_cli()
    issue_key_via_cli(customer_id)
    _, key_prefix = latest_key_id_and_prefix(env["db_path"], customer_id)

    result = runner.invoke(app, ["show", key_prefix])
    stdout, stderr = buffers()
    assert result.exit_code == 0, stderr

    for label in (
        "Prefix:",
        "Customer:",
        "Status:",
        "RPM:",
        "Price:",
        "Billing:",
        "Paid Until:",
        "Created:",
        "Last Used:",
        "Age:",
        "Notes:",
    ):
        assert label in stdout
    assert f"Key: {key_prefix}" in stdout
    assert "active" in stdout
    assert "60/min" in stdout
    assert "R$ 149,00/month" in stdout
    assert "today" in stdout
    assert "—" in stdout
    assert "events:" not in stdout


def test_show_customer_does_not_leak_ciphertext(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """LGPD: tax_id_encrypted must NEVER reach stdout/stderr."""
    freeze_today(date(2026, 4, 16))
    customer_id = create_customer_via_cli()
    result = runner.invoke(app, ["show", customer_id])
    stdout, stderr = buffers()
    assert result.exit_code == 0
    combined = stdout + stderr
    assert "gAAAAA" not in combined
    assert not re.search(r"[A-Za-z0-9+/=_-]{60,}", combined)


def test_show_audit_tail_renders_newest_first_with_details(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """``--audit-tail N`` surfaces the per-key event log, newest first,
    rendering details_json verbatim."""
    freeze_today(date(2026, 4, 16))
    customer_id = create_customer_via_cli()
    issue_key_via_cli(customer_id)
    key_id, key_prefix = latest_key_id_and_prefix(env["db_path"], customer_id)

    conn = sqlite3.connect(env["db_path"])
    try:
        conn.execute(
            "INSERT INTO audit_log "
            "(occurred_at, actor, action, target_type, target_id, details_json) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                "2099-04-16T09:15:00Z",
                "cli:ricardo",
                "update_key",
                "api_key",
                key_id,
                '{"notes": "bumped rpm"}',
            ),
        )
        conn.commit()
    finally:
        conn.close()

    result = runner.invoke(app, ["show", key_prefix, "--audit-tail", "20"])
    stdout, stderr = buffers()
    assert result.exit_code == 0, stderr
    assert "UPDATE_KEY" in stdout
    assert "ISSUE_KEY" in stdout
    assert stdout.index("UPDATE_KEY") < stdout.index("ISSUE_KEY")
    assert '{"notes": "bumped rpm"}' in stdout


def test_show_format_json_emits_fixed_key_order(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    """AC8 — JSON shape is a machine contract: customer and key targets
    each have a fixed key order. A regression to ``dict(sorted(...))`` is
    caught here."""
    freeze_today(date(2026, 4, 16))
    customer_id = create_customer_via_cli()
    issue_key_via_cli(customer_id)
    key_id, key_prefix = latest_key_id_and_prefix(env["db_path"], customer_id)

    result = runner.invoke(app, ["show", customer_id, "--format", "json"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    parsed = json.loads(stdout)
    assert parsed["target_type"] == "customer"
    assert list(parsed["target"].keys()) == [
        "id",
        "name",
        "email",
        "tax_id_kind",
        "key_count",
        "registered_at",
        "notes",
    ]

    clear_buffers()
    result = runner.invoke(app, ["show", key_prefix, "--format", "json"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    parsed = json.loads(stdout)
    assert parsed["target_type"] == "api_key"
    target = parsed["target"]
    assert list(target.keys()) == [
        "id",
        "customer_id",
        "customer_name",
        "key_prefix",
        "status",
        "billing_mode",
        "rate_limit_rpm",
        "price_monthly",
        "paid_until",
        "created_at",
        "last_used_at",
        "age_days",
        "notes",
    ]
    assert "key_hash" not in target
    assert target["id"] == key_id


def test_show_not_found_exits_3(env: dict[str, str]) -> None:
    """Unknown prefix, unknown customer-id, and tombstoned customer all
    return the same 'not found' envelope at exit 3."""
    result = runner.invoke(app, ["show", "NOPENOPE"])
    _, stderr = buffers()
    assert result.exit_code == 3
    assert "Not found: no customer or key matches 'NOPENOPE'" in stderr

    customer_id = create_customer_via_cli()
    conn = sqlite3.connect(env["db_path"])
    try:
        conn.execute(
            "UPDATE customer SET deleted_at = ? WHERE id = ?",
            ("2026-04-20T00:00:00Z", customer_id),
        )
        conn.commit()
    finally:
        conn.close()
    result = runner.invoke(app, ["show", customer_id])
    _, stderr = buffers()
    assert result.exit_code == 3
    assert "Not found" in stderr
