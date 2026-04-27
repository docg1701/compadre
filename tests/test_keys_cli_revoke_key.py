"""End-to-end CLI tests for ``compadre-keys revoke-key`` (Story 1.6).

Lean by design (test-suite review 2026-04-25): one --yes happy path
with audit details, one already-revoked / not-found, one reason-validation.
"""

from __future__ import annotations

import json
import sqlite3

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import buffers, clear_buffers, runner, seed_by_prefix


def test_revoke_key_yes_flag_revokes_with_audit(env: dict[str, str]) -> None:
    """``--yes`` revokes the key, stamps audit with reason verbatim."""
    _, _, prefix = seed_by_prefix(env["db_path"])
    result = runner.invoke(
        app, ["revoke-key", prefix, "--reason", "compromised", "--yes"]
    )
    _, stderr = buffers()
    assert result.exit_code == 0, stderr

    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute("SELECT status FROM api_key").fetchone()
        assert row[0] == "revoked"
        audit = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'revoke_key'"
        ).fetchone()
        assert json.loads(audit[0]) == {"reason": "compromised"}
    finally:
        conn.close()


def test_revoke_key_already_revoked_or_missing_exits_3(env: dict[str, str]) -> None:
    """Revoking a revoked key + revoking an unknown prefix both exit 3."""
    _, _, prefix = seed_by_prefix(env["db_path"], status="revoked")
    result = runner.invoke(app, ["revoke-key", prefix, "--reason", "x", "--yes"])
    assert result.exit_code == 3
    clear_buffers()

    result = runner.invoke(app, ["revoke-key", "NOPENOPE", "--reason", "x", "--yes"])
    assert result.exit_code == 3


def test_revoke_key_rejects_invalid_reason(env: dict[str, str]) -> None:
    """--reason must be non-empty, no control bytes."""
    _, _, prefix = seed_by_prefix(env["db_path"])
    for bad in ("", "   ", "with\x00nul"):
        result = runner.invoke(app, ["revoke-key", prefix, "--reason", bad, "--yes"])
        assert result.exit_code == 2
        clear_buffers()
