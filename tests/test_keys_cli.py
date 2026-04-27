"""End-to-end CLI tests for ``compadre-keys create-customer`` (Story 1.1).

Lean by design (test-suite review 2026-04-25): one happy path, one CPF
checksum rejection, one missing-encryption-key envelope.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from typer.testing import CliRunner

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import buffers as _buffers
from tests._keys_cli_helpers import clear_buffers as _clear_buffers

runner = CliRunner()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    key = Fernet.generate_key().decode()
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("KEY_REGISTRY_DB_PATH", str(db_path))
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", key)
    return {"db_path": str(db_path), "key": key}


def test_create_customer_happy_path_writes_row_and_audit(env: dict[str, str]) -> None:
    """Happy path: customer row inserted, ID echoed, audit row written
    with NO PII in details."""
    result = runner.invoke(
        app,
        [
            "create-customer",
            "--name",
            "Helena Ribeiro",
            "--email",
            "helena@clinica.com",
            "--cpf",
            "123.456.789-09",
            "--notes",
            "5 bots",
        ],
    )
    stdout, _ = _buffers()
    assert result.exit_code == 0
    cust_id = re.search(r"cust_[0-9a-z]{26}", stdout)
    assert cust_id is not None
    cid = cust_id.group(0)

    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute("SELECT * FROM customer WHERE id = ?", (cid,)).fetchone()
        assert row is not None  # row present
        # Audit row exists with no PII in details_json.
        audit = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'create_customer'"
        ).fetchone()[0]
        assert "Helena" not in audit
        assert "helena@clinica.com" not in audit
    finally:
        conn.close()


def test_create_customer_invalid_cpf_exits_4(env: dict[str, str]) -> None:
    """CPF with bad checksum is rejected with exit 4 (validation)."""
    result = runner.invoke(
        app,
        [
            "create-customer",
            "--name",
            "X",
            "--email",
            "x@x.com",
            "--cpf",
            "111.222.333-99",
        ],  # invalid checksum
    )
    # B1 — checksum failure is a usage-domain validation error, exit 2.
    # Exit 4 is reserved for ``rotate-encryption-key`` Fernet shape failure.
    assert result.exit_code == 2


def test_create_customer_missing_encryption_key_exits_6(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing KEY_REGISTRY_ENCRYPTION_KEY → exit 6 with config-error envelope."""
    monkeypatch.delenv("KEY_REGISTRY_ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("KEY_REGISTRY_DB_PATH", str(tmp_path / "r.db"))
    _clear_buffers()
    result = runner.invoke(
        app,
        [
            "create-customer",
            "--name",
            "X",
            "--email",
            "x@x.com",
            "--cpf",
            "123.456.789-09",
        ],
    )
    assert result.exit_code == 6
