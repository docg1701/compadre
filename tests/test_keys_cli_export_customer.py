"""End-to-end CLI tests for ``compadre-keys export-customer`` (Story 4.1).

Lean by design (test-suite review 2026-04-25): one happy path that
verifies LGPD export shape (decrypted tax_id present + ciphertext NEVER
on stdout), one not-found.
"""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime

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
from tests._keys_cli_helpers import buffers, clear_buffers, runner


def _seed_with_real_tax_id(env: dict[str, str]) -> str:
    """Insert one customer with a Fernet-encrypted tax_id (the export path
    needs a decryptable blob, which the prefix-seed placeholder isn't)."""
    conn = get_connection(env["db_path"])
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
                tax_id_encrypted=encrypt_tax_id("12345678909", SecretStr(env["key"])),
                notes="",
                registered_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
                deleted_at=None,
            ),
            actor="cli:tester",
        )
    finally:
        conn.close()
    return cid


def test_export_customer_emits_decrypted_tax_id_no_ciphertext_in_stdout(
    env: dict[str, str],
) -> None:
    """LGPD: export decrypts tax_id; the ciphertext blob must NEVER appear
    on stdout (anywhere)."""
    cid = _seed_with_real_tax_id(env)

    result = runner.invoke(app, ["export-customer", cid])
    stdout, _ = buffers()
    assert result.exit_code == 0
    parsed = json.loads(stdout)
    assert parsed["id"] == cid
    assert parsed["tax_id"] == "123.456.789-09"
    assert "gAAAAA" not in stdout
    assert "tax_id_encrypted" not in parsed
    assert not re.search(r"[A-Za-z0-9+/=_-]{60,}", stdout)


def test_export_customer_not_found_exits_3(env: dict[str, str]) -> None:
    result = runner.invoke(app, ["export-customer", "cust_00000000000000000000000000"])
    assert result.exit_code == 3
    clear_buffers()


def test_export_customer_default_omits_audit_events(env: dict[str, str]) -> None:
    """Story 5.1 Gap 6: without --include-audit, the envelope has no
    ``audit_events`` key (LGPD minimum-necessary default)."""
    cid = _seed_with_real_tax_id(env)
    result = runner.invoke(app, ["export-customer", cid])
    stdout, _ = buffers()
    assert result.exit_code == 0
    parsed = json.loads(stdout)
    assert "audit_events" not in parsed


def test_export_customer_with_include_audit_emits_filtered_events(
    env: dict[str, str],
) -> None:
    """Story 5.1 Gap 6: with --include-audit, ``audit_events`` is a non-empty
    list whose entries each carry ``action``, ``actor``, ``occurred_at``;
    every ``details`` payload (if present) passes the forbidden-keys filter."""
    cid = _seed_with_real_tax_id(env)
    result = runner.invoke(app, ["export-customer", cid, "--include-audit"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    parsed = json.loads(stdout)
    assert "audit_events" in parsed
    events = parsed["audit_events"]
    assert isinstance(events, list)
    assert len(events) >= 1  # at least the create_customer row
    for event in events:
        assert {"action", "actor", "occurred_at"}.issubset(event.keys())
        details = event.get("details", {})
        forbidden = {"cpf", "cnpj", "tax_id", "name", "email", "key_plaintext"}
        assert not (forbidden & set(details.keys()))
