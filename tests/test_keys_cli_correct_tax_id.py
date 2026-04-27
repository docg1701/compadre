"""End-to-end CLI tests for ``compadre-keys correct-tax-id`` (Story 5.1 Gap 7).

Verifies the LGPD Art. 18 III correction primitive: the encrypted tax-ID
is replaced atomically; ``customer_id`` and every ``api_key.customer_id``
foreign key remain stable; the audit row records old/new ``tax_id_kind``
and the operator-supplied reason; the new ciphertext round-trips through
``decrypt_tax_id`` to the new value."""

from __future__ import annotations

import json
import sqlite3

from pydantic import SecretStr

from compadre.keys.cli.app import app
from compadre.keys.crypto import decrypt_tax_id, encrypt_tax_id
from compadre.keys.db import (
    _apply_migrations,
    generate_customer_id,
    generate_key_id,
    get_connection,
    insert_api_key,
    insert_customer,
)
from compadre.keys.models import ApiKeyRecord, CustomerRecord
from tests._keys_cli_helpers import buffers, clear_buffers, runner


def _seed(env: dict[str, str]) -> tuple[str, str]:
    """Seed one customer (CPF '123.456.789-09') + one api_key. Returns
    ``(customer_id, key_id)``."""
    from datetime import UTC, datetime

    conn = get_connection(env["db_path"])
    try:
        _apply_migrations(conn)
        cid = generate_customer_id()
        insert_customer(
            conn,
            CustomerRecord(
                id=cid,
                name="Marcos Silva",
                email="m@x.com",
                tax_id_kind="cpf",
                tax_id_encrypted=encrypt_tax_id("12345678909", SecretStr(env["key"])),
                notes="",
                registered_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC),
                deleted_at=None,
            ),
            actor="cli:tester",
        )
        kid = generate_key_id()
        insert_api_key(
            conn,
            ApiKeyRecord(
                id=kid,
                customer_id=cid,
                key_hash="a" * 64,
                key_prefix="Pq_corr1",
                rate_limit_rpm=60,
                status="active",
                billing_mode="manual",
                price_monthly=149.0,
                paid_until=None,
                created_at=datetime(2026, 4, 1, 12, 0, 0, tzinfo=UTC),
                last_used_at=None,
                notes="",
            ),
            actor="cli:tester",
        )
    finally:
        conn.close()
    return cid, kid


def test_correct_tax_id_replaces_value_preserves_id_and_audits(
    env: dict[str, str],
) -> None:
    """Happy path: --new-cpf replaces ciphertext; customer_id stable;
    api_key.customer_id stable; audit row carries old/new kind + reason
    but never the tax-ID value (FR37)."""
    cid, kid = _seed(env)
    new_cpf = "111.444.777-35"  # valid CPF (different number)
    result = runner.invoke(
        app,
        [
            "correct-tax-id",
            cid,
            "--new-cpf",
            new_cpf,
            "--reason",
            "operator typo",
            "--yes",
        ],
    )
    stdout, stderr = buffers()
    assert result.exit_code == 0, stderr
    assert "Tax-ID corrected" in stdout

    conn = sqlite3.connect(env["db_path"])
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT id, tax_id_kind, tax_id_encrypted FROM customer WHERE id = ?",
            (cid,),
        ).fetchone()
        assert row["id"] == cid
        assert row["tax_id_kind"] == "cpf"
        decrypted = decrypt_tax_id(
            bytes(row["tax_id_encrypted"]), SecretStr(env["key"])
        )
        assert decrypted == "11144477735"  # canonical strip of new_cpf
        # FK preserved.
        key_row = conn.execute(
            "SELECT customer_id FROM api_key WHERE id = ?", (kid,)
        ).fetchone()
        assert key_row["customer_id"] == cid
        # Audit row.
        audit = conn.execute(
            "SELECT details_json FROM audit_log "
            "WHERE action = 'correct_tax_id' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    details = json.loads(audit[0])
    assert details == {
        "new_kind": "cpf",
        "old_kind": "cpf",
        "reason": "operator typo",
    }
    assert "11144477735" not in audit[0]
    assert "12345678909" not in audit[0]


def test_correct_tax_id_kind_change_cpf_to_cnpj(env: dict[str, str]) -> None:
    """Kind change recorded distinctly in audit."""
    cid, _ = _seed(env)
    result = runner.invoke(
        app,
        [
            "correct-tax-id",
            cid,
            "--new-cnpj",
            "11.222.333/0001-81",
            "--reason",
            "switched to CNPJ",
            "--yes",
        ],
    )
    _, stderr = buffers()
    assert result.exit_code == 0, stderr
    conn = sqlite3.connect(env["db_path"])
    try:
        row = conn.execute(
            "SELECT tax_id_kind FROM customer WHERE id = ?", (cid,)
        ).fetchone()
        audit = conn.execute(
            "SELECT details_json FROM audit_log "
            "WHERE action = 'correct_tax_id' ORDER BY id DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "cnpj"
    details = json.loads(audit[0])
    assert details["old_kind"] == "cpf"
    assert details["new_kind"] == "cnpj"


def test_correct_tax_id_invalid_checksum_exits_2(env: dict[str, str]) -> None:
    """B1 — bad CPF / CNPJ checksum exits 2 (usage-domain validation),
    not 4. Rejected input never echoed (anti-enumeration FR37/NFR16)."""
    cid, _ = _seed(env)
    result = runner.invoke(
        app,
        ["correct-tax-id", cid, "--new-cpf", "00000000000", "--reason", "x", "--yes"],
    )
    _, stderr = buffers()
    assert result.exit_code == 2
    assert "00000000000" not in stderr


def test_correct_tax_id_rejects_invalid_inputs(env: dict[str, str]) -> None:
    """Mutex / oversize-reason / both-tax-id-flags / bad-control → exit 2."""
    cid, _ = _seed(env)
    for bad in (
        # Both tax-id flags.
        [
            "correct-tax-id",
            cid,
            "--new-cpf",
            "111.444.777-35",
            "--new-cnpj",
            "11.222.333/0001-81",
            "--reason",
            "x",
            "--yes",
        ],
        # Neither tax-id flag.
        ["correct-tax-id", cid, "--reason", "x", "--yes"],
        # NUL in reason.
        [
            "correct-tax-id",
            cid,
            "--new-cpf",
            "111.444.777-35",
            "--reason",
            "bad\x00byte",
            "--yes",
        ],
        # Newline in reason.
        [
            "correct-tax-id",
            cid,
            "--new-cpf",
            "111.444.777-35",
            "--reason",
            "two\nlines",
            "--yes",
        ],
    ):
        clear_buffers()
        result = runner.invoke(app, bad)
        assert result.exit_code == 2, bad


def test_correct_tax_id_missing_customer_exits_3(env: dict[str, str]) -> None:
    result = runner.invoke(
        app,
        [
            "correct-tax-id",
            "cust_00000000000000000000000000",
            "--new-cpf",
            "111.444.777-35",
            "--reason",
            "x",
            "--yes",
        ],
    )
    assert result.exit_code == 3


def test_correct_tax_id_invalid_customer_id_shape_exits_2(
    env: dict[str, str],
) -> None:
    """Story 5.1 review patch: parity with export-customer / delete-customer
    — a malformed customer-ID exits 2 BEFORE settings load / encryption /
    DB connection, so a typo never opens a connection."""
    result = runner.invoke(
        app,
        [
            "correct-tax-id",
            "not-a-customer-id",
            "--new-cpf",
            "111.444.777-35",
            "--reason",
            "x",
            "--yes",
        ],
    )
    assert result.exit_code == 2
