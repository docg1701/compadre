"""End-to-end CLI tests for ``compadre-keys delete-customer`` (Story 4.1).

Lean by design (test-suite review 2026-04-25): LGPD purge contract is
the survivor — customer + keys hard-deleted, tombstone written, audit
emitted with no PII.
"""

from __future__ import annotations

import json
import sqlite3

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import buffers, clear_buffers, runner, seed_by_prefix


def test_delete_customer_hard_purges_rows_writes_audit_no_pii(
    env: dict[str, str],
) -> None:
    """LGPD purge: customer scrubbed (PII zeroed) + tombstone stamped +
    api_key rows deleted + audit row written with no email/name in details."""
    cid, _kid, _ = seed_by_prefix(env["db_path"], customer_email="helena@clinica.com")
    result = runner.invoke(app, ["delete-customer", cid, "--hard", "--yes"])
    _, stderr = buffers()
    assert result.exit_code == 0, stderr

    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT name, email, deleted_at FROM customer WHERE id = ?", (cid,)
        ).fetchone()
        assert row[0] == ""
        assert row[1] == ""
        assert row[2] is not None
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM api_key WHERE customer_id = ?", (cid,)
            ).fetchone()[0]
            == 0
        )
        audit = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'delete_customer'"
        ).fetchone()
        details = json.loads(audit[0])
        assert "email" not in details
        assert "name" not in details
        assert details.get("keys_purged") == 1
    finally:
        conn.close()


def test_delete_customer_not_found_exits_3(env: dict[str, str]) -> None:
    result = runner.invoke(
        app, ["delete-customer", "cust_00000000000000000000000000", "--hard", "--yes"]
    )
    assert result.exit_code == 3
    clear_buffers()
