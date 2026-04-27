"""End-to-end CLI tests for ``compadre-keys at-risk`` (Story 3.2).

Lean by design (test-suite review 2026-04-25): one happy path with
content assertion, one --format json shape.
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
from tests._keys_cli_helpers import buffers, runner


def _seed_at_risk(db_path: str) -> None:
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
        for prefix, paid_until in (
            ("RISKY1AA", "2026-04-18"),
            ("RISKY2BB", "2026-04-20"),
        ):
            insert_api_key(
                conn,
                ApiKeyRecord(
                    id=generate_key_id(),
                    customer_id=cid,
                    key_hash=prefix.lower() * 8,
                    key_prefix=prefix,
                    rate_limit_rpm=60,
                    status="active",
                    billing_mode="manual",
                    price_monthly=149.0,
                    paid_until=paid_until,
                    created_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC),
                    last_used_at=None,
                    notes="",
                ),
                actor="cli:tester",
            )
    finally:
        conn.close()


def test_at_risk_table_lists_keys_within_window(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    freeze_today(date(2026, 4, 16))
    _seed_at_risk(env["db_path"])
    result = runner.invoke(app, ["at-risk", "--window", "5d"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "RISKY1AA" in stdout
    assert "RISKY2BB" in stdout


def test_at_risk_format_json_emits_array(
    env: dict[str, str], freeze_today: Callable[[date], None]
) -> None:
    freeze_today(date(2026, 4, 16))
    _seed_at_risk(env["db_path"])
    result = runner.invoke(app, ["at-risk", "--window", "5d", "--format", "json"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    parsed = json.loads(stdout)
    assert isinstance(parsed, (dict, list))
    text = json.dumps(parsed)
    assert "RISKY1AA" in text
    assert "RISKY2BB" in text
