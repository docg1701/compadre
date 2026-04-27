"""End-to-end CLI tests for ``compadre-keys rotate-key`` (Story 1.6 / 5.1).

Lean by design (test-suite review 2026-04-25): one happy path that
proves the security-critical contract (new plaintext, new hash, old
plaintext no longer matches), one already-revoked / not-found, and
the Story 5.1 ``--yes`` confirmation gate (closed-stdin abort hint).
"""

from __future__ import annotations

import re
import sqlite3

from compadre.keys.cli.app import app
from compadre.keys.tokens import hash_api_key
from tests._keys_cli_helpers import buffers, clear_buffers, runner, seed_by_prefix


def test_rotate_key_yes_flag_emits_new_plaintext_and_invalidates_old(
    env: dict[str, str],
) -> None:
    """Rotation: same key_id, new key_hash, new key_prefix; old hash no
    longer present. The plaintext is shown exactly once on stdout."""
    _, kid, prefix = seed_by_prefix(env["db_path"])
    old_hash = "a" * 64

    result = runner.invoke(app, ["rotate-key", prefix, "--yes"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    new_plaintext_match = re.search(r"cmpd_live_[A-Za-z0-9_-]{43}", stdout)
    assert new_plaintext_match is not None
    new_plaintext = new_plaintext_match.group(0)
    assert stdout.count(new_plaintext) == 1

    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT id, key_hash FROM api_key WHERE id = ?", (kid,)
        ).fetchone()
        assert row[0] == kid  # same key_id
        assert row[1] != old_hash
        assert row[1] == hash_api_key(new_plaintext)
    finally:
        conn.close()


def test_rotate_key_revoked_or_missing_exits_3(env: dict[str, str]) -> None:
    _, _, prefix = seed_by_prefix(env["db_path"], status="revoked")
    result = runner.invoke(app, ["rotate-key", prefix, "--yes"])
    assert result.exit_code == 3
    clear_buffers()

    result = runner.invoke(app, ["rotate-key", "NOPENOPE", "--yes"])
    assert result.exit_code == 3


def test_rotate_key_without_yes_aborts_with_scripting_hint(
    env: dict[str, str],
) -> None:
    """Story 5.1 Gap 5: closed-stdin invocation without --yes aborts at
    the confirmation gate, exits 2, and emits the scripting hint."""
    _, _, prefix = seed_by_prefix(env["db_path"])
    result = runner.invoke(app, ["rotate-key", prefix], input="")
    _, stderr = buffers()
    assert result.exit_code == 2
    assert "Aborted." in stderr
    assert "--yes" in stderr
