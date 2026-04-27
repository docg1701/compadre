"""End-to-end CLI tests for ``compadre-keys rotate-encryption-key`` (Story 4.2).

Lean by design (test-suite review 2026-04-25): the security-critical
contract is "all rows re-encrypted under the new key OR none, atomically".
That property is exercised end-to-end at the DB layer in
``test_keys_db.py::test_rotate_encryption_keys_happy_path_and_rollback``.
The CLI wrapper kept here covers: --yes happy path produces audit row +
prints new key (NFR12 operator hint); bad new-key shape exits 4.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime

from cryptography.fernet import Fernet
from pydantic import SecretStr

from compadre.keys.cli.app import app
from compadre.keys.crypto import encrypt_tax_id
from compadre.keys.db import (
    _apply_migrations,
    generate_customer_id,
    get_connection,
    insert_customer,
)
from compadre.keys.models import CustomerRecord
from tests._keys_cli_helpers import buffers, runner


def _seed_with_real_tax_id(env: dict[str, str]) -> None:
    conn = get_connection(env["db_path"])
    try:
        _apply_migrations(conn)
        insert_customer(
            conn,
            CustomerRecord(
                id=generate_customer_id(),
                name="Helena",
                email="h@x.com",
                tax_id_kind="cpf",
                tax_id_encrypted=encrypt_tax_id("12345678909", SecretStr(env["key"])),
                notes="",
                registered_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
                deleted_at=None,
            ),
            actor="cli:tester",
        )
    finally:
        conn.close()


def test_rotate_encryption_key_yes_writes_audit_and_echoes_new_key(
    env: dict[str, str],
) -> None:
    """--yes runs the rotation, writes one audit row, echoes the new key
    on stdout (single sanctioned key-material echo per Story 4.2)."""
    _seed_with_real_tax_id(env)
    new_key = Fernet.generate_key().decode()

    result = runner.invoke(
        app, ["rotate-encryption-key", "--new-key", new_key, "--yes"]
    )
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert new_key in stdout

    conn = sqlite3.connect(env["db_path"])
    try:
        count = conn.execute(
            "SELECT COUNT(*) FROM audit_log WHERE action = 'rotate_encryption_key'"
        ).fetchone()[0]
        assert count == 1
    finally:
        conn.close()


def test_rotate_encryption_key_bad_new_key_exits_4_pre_db(
    env: dict[str, str],
) -> None:
    """A malformed Fernet new-key value is rejected without opening the
    DB (exit 4 — validation error, no audit row written)."""
    result = runner.invoke(
        app, ["rotate-encryption-key", "--new-key", "not-a-fernet-key", "--yes"]
    )
    assert result.exit_code == 4
