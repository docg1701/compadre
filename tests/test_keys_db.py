"""Tests for ``compadre.keys.db`` — schema, rollback, encryption rotation.

Lean by design (test-suite review 2026-04-25): per-CRUD-helper unit tests
deleted (covered end-to-end by CLI integration tests). Kept the cases that
ONLY a DB-layer test can cover: schema/migration shape, transaction
rollback under forced failure, encryption-rotation atomicity, and the hot
auth-path index.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from compadre.keys.crypto import decrypt_tax_id, encrypt_tax_id
from compadre.keys.db import (
    _SCHEMA_VERSION,
    _apply_migrations,
    delete_customer_hard,
    get_connection,
    insert_customer,
    lookup_key_auth,
    rotate_encryption_keys,
)
from compadre.keys.models import CustomerRecord, TaxIdKind
from compadre.keys.tokens import hash_api_key
from tests._keys_fixtures import seed_customer_and_key


def _make_record(cid: str = "cust_01abcdef") -> CustomerRecord:
    return CustomerRecord(
        id=cid,
        name="Helena Ribeiro",
        email="helena@clinica.com",
        tax_id_kind="cpf",
        tax_id_encrypted=b"ciphertext",
        notes="",
        registered_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
        deleted_at=None,
    )


def _seed_active(
    conn: sqlite3.Connection, *, cid: str, canonical: str, kind: str, key: SecretStr
) -> None:
    insert_customer(
        conn,
        CustomerRecord(
            id=cid,
            name="Ana",
            email=f"{cid}@x.com",
            tax_id_kind=cast(TaxIdKind, kind),
            tax_id_encrypted=encrypt_tax_id(canonical, key),
            notes="",
            registered_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
            deleted_at=None,
        ),
        actor="cli:tester",
    )


def test_migrations_create_schema_with_indexes(
    registry_conn: sqlite3.Connection,
) -> None:
    """Fresh DB → all tables + every required index + schema_version row per file."""
    tables = {
        row[0]
        for row in registry_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    assert {"customer", "api_key", "audit_log", "schema_version"} <= tables

    indexes = {
        row[0]
        for row in registry_conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    }
    assert {
        "idx_api_key_hash",
        "idx_api_key_customer",
        "idx_api_key_sweep",
        "idx_audit_occurred",
    } <= indexes

    rows = registry_conn.execute(
        "SELECT version FROM schema_version ORDER BY version"
    ).fetchall()
    assert len(rows) == _SCHEMA_VERSION
    assert rows[-1]["version"] == _SCHEMA_VERSION

    # Idempotency: re-running must not error or duplicate rows.
    _apply_migrations(registry_conn)
    assert (
        registry_conn.execute("SELECT COUNT(*) FROM schema_version").fetchone()[0]
        == _SCHEMA_VERSION
    )


def test_v002_rate_limit_check_constraint_rejects_out_of_range(
    registry_conn: sqlite3.Connection,
) -> None:
    """v002 — defense-in-depth against direct SQL edits / malformed restores.

    The CLI already caps rate_limit_rpm to 1..10000; the CHECK constraint is
    the second line of defence so a hand-edited .db file or backup restored
    under a forked schema cannot plant a rogue value.
    """
    registry_conn.execute(
        "INSERT INTO customer "
        "(id, name, email, tax_id_kind, tax_id_encrypted, registered_at) "
        "VALUES ('cust_x', 'X', 'x@x.com', 'cpf', x'00', '2026-04-25T00:00:00Z')"
    )
    registry_conn.commit()
    for bad_rpm in (0, 10001):
        with pytest.raises(sqlite3.IntegrityError, match="CHECK"):
            registry_conn.execute(
                "INSERT INTO api_key "
                "(id, customer_id, key_hash, key_prefix, rate_limit_rpm, "
                "status, billing_mode, created_at) VALUES "
                "(?, 'cust_x', ?, 'p', ?, 'active', 'manual', "
                "'2026-04-25T00:00:00Z')",
                (f"key_{bad_rpm}", f"h{bad_rpm}", bad_rpm),
            )


def test_insert_customer_rolls_back_when_audit_fails(
    registry_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Audit-log INSERT failure must roll back the customer INSERT."""
    import compadre.keys.audit as audit_module

    def _boom(*_args: object, **_kw: object) -> None:
        raise sqlite3.IntegrityError("forced")

    monkeypatch.setattr(audit_module, "log_event", _boom)

    with pytest.raises(sqlite3.IntegrityError):
        insert_customer(registry_conn, _make_record(), actor="cli:tester")

    assert registry_conn.execute("SELECT COUNT(*) FROM customer").fetchone()[0] == 0


def test_v003_tax_id_lookup_unique_rejects_duplicate_active(
    registry_conn: sqlite3.Connection,
) -> None:
    """B2 — partial UNIQUE INDEX on ``tax_id_lookup`` rejects two active
    customers with the same tax-ID lookup digest. Tombstoned rows
    (``deleted_at IS NOT NULL``) and NULL-lookup rows (pre-v3 backfill)
    are excluded from the constraint.
    """
    same_lookup = b"\xaa" * 32
    insert_customer(
        registry_conn,
        _make_record(),
        actor="cli:tester",
        tax_id_lookup=same_lookup,
    )

    second = CustomerRecord(
        id="cust_99zzzzzz",
        name="Bruno",
        email="bruno@x.com",
        tax_id_kind="cpf",
        tax_id_encrypted=b"different-ciphertext",
        notes="",
        registered_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
        deleted_at=None,
    )
    with pytest.raises(sqlite3.IntegrityError, match="tax_id_lookup"):
        insert_customer(
            registry_conn, second, actor="cli:tester", tax_id_lookup=same_lookup
        )

    # Distinct lookup → accepted.
    other = CustomerRecord(
        id="cust_77yyyyyy",
        name="Carla",
        email="carla@x.com",
        tax_id_kind="cpf",
        tax_id_encrypted=b"yet-another",
        notes="",
        registered_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
        deleted_at=None,
    )
    insert_customer(
        registry_conn, other, actor="cli:tester", tax_id_lookup=b"\xbb" * 32
    )
    assert registry_conn.execute("SELECT COUNT(*) FROM customer").fetchone()[0] == 2


def test_v003_tax_id_lookup_unique_excludes_tombstones_and_null(
    registry_conn: sqlite3.Connection,
) -> None:
    """B2 — partial UNIQUE excludes tombstones and NULL-lookup rows so:
    1. A pre-v3 row (``tax_id_lookup IS NULL``) does NOT block a new row
       with a matching backfilled lookup until the operator backfills.
    2. A hard-deleted CPF can be re-registered later (LGPD).
    """
    # Pre-v3 row (NULL lookup).
    insert_customer(
        registry_conn,
        _make_record(),
        actor="cli:tester",
        tax_id_lookup=None,
    )
    # New row with a non-NULL lookup digest — must be accepted (no
    # collision with NULL).
    new_row = CustomerRecord(
        id="cust_88wwwwww",
        name="Diana",
        email="diana@x.com",
        tax_id_kind="cpf",
        tax_id_encrypted=b"x",
        notes="",
        registered_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
        deleted_at=None,
    )
    insert_customer(
        registry_conn, new_row, actor="cli:tester", tax_id_lookup=b"\xcc" * 32
    )

    # Tombstone the new_row → its lookup must be cleared (helper does
    # this) so a third registration with the same digest is allowed.
    from compadre.keys.db import delete_customer_hard

    delete_customer_hard(registry_conn, "cust_88wwwwww", actor="cli:tester")
    re_registered = CustomerRecord(
        id="cust_55vvvvvv",
        name="Diana 2",
        email="diana2@x.com",
        tax_id_kind="cpf",
        tax_id_encrypted=b"y",
        notes="",
        registered_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
        deleted_at=None,
    )
    insert_customer(
        registry_conn, re_registered, actor="cli:tester", tax_id_lookup=b"\xcc" * 32
    )


def test_backfill_tax_id_lookup_is_idempotent(tmp_path: Path) -> None:
    """B2 — backfill helper populates NULL lookups, leaves filled ones
    untouched, skips tombstones, and is safe to call repeatedly.
    """
    from compadre.keys.crypto import compute_tax_id_lookup
    from compadre.keys.db import backfill_tax_id_lookup_if_needed

    key = SecretStr(Fernet.generate_key().decode())
    conn = get_connection(tmp_path / "backfill.db")
    try:
        _apply_migrations(conn)
        # Two active rows (NULL lookup), one to be tombstoned.
        for cid, canonical in (
            ("cust_01aaaaaa", "12345678909"),
            ("cust_02bbbbbb", "11144477735"),
            ("cust_03cccccc", "39053344705"),
        ):
            _seed_active(conn, cid=cid, canonical=canonical, kind="cpf", key=key)
        from compadre.keys.db import delete_customer_hard

        delete_customer_hard(conn, "cust_03cccccc", actor="cli:tester")

        # First call backfills the two active rows.
        first = backfill_tax_id_lookup_if_needed(conn, encryption_key=key)
        assert first == 2

        # Second call is a no-op.
        second = backfill_tax_id_lookup_if_needed(conn, encryption_key=key)
        assert second == 0

        # Tombstone's lookup remains NULL.
        row = conn.execute(
            "SELECT tax_id_lookup FROM customer WHERE id = ?", ("cust_03cccccc",)
        ).fetchone()
        assert row["tax_id_lookup"] is None

        # Active rows have the correct deterministic digest.
        for cid, canonical in (
            ("cust_01aaaaaa", "12345678909"),
            ("cust_02bbbbbb", "11144477735"),
        ):
            row = conn.execute(
                "SELECT tax_id_lookup FROM customer WHERE id = ?", (cid,)
            ).fetchone()
            assert bytes(row["tax_id_lookup"]) == compute_tax_id_lookup(canonical, key)
    finally:
        conn.close()


def test_rotate_encryption_keys_happy_path_and_rollback(tmp_path: Path) -> None:
    """Story 4.2 atomicity: every active row re-encrypted under the new key,
    OR — on a wrong old key — every row left byte-identical and no audit."""
    old_key = SecretStr(Fernet.generate_key().decode())
    new_key = SecretStr(Fernet.generate_key().decode())

    # Happy: all active rows re-encrypted; old key no longer decrypts; one audit.
    conn = get_connection(tmp_path / "happy.db")
    try:
        _apply_migrations(conn)
        for cid, canonical, kind in (
            ("cust_01aaaaaa", "12345678909", "cpf"),
            ("cust_01bbbbbb", "12345678000195", "cnpj"),
        ):
            _seed_active(conn, cid=cid, canonical=canonical, kind=kind, key=old_key)

        result = rotate_encryption_keys(
            conn, old_key=old_key, new_key=new_key, actor="cli:tester"
        )
        assert result.rows_re_encrypted == 2

        rows = dict(
            conn.execute("SELECT id, tax_id_encrypted FROM customer").fetchall()
        )
        assert decrypt_tax_id(bytes(rows["cust_01aaaaaa"]), new_key) == "12345678909"
        assert decrypt_tax_id(bytes(rows["cust_01bbbbbb"]), new_key) == "12345678000195"
        with pytest.raises(InvalidToken):
            decrypt_tax_id(bytes(rows["cust_01aaaaaa"]), old_key)

        audit = conn.execute(
            "SELECT details_json FROM audit_log WHERE action = 'rotate_encryption_key'"
        ).fetchall()
        assert len(audit) == 1
        assert json.loads(audit[0]["details_json"]) == {"rows_re_encrypted": 2}
    finally:
        conn.close()

    # Rollback: a wrong old_key leaves every row byte-identical, zero audit rows.
    conn = get_connection(tmp_path / "rollback.db")
    try:
        _apply_migrations(conn)
        for idx in range(3):
            _seed_active(
                conn,
                cid=f"cust_01rb{idx:02d}xxxxxxx",
                canonical="12345678909",
                kind="cpf",
                key=old_key,
            )
        pre = dict(conn.execute("SELECT id, tax_id_encrypted FROM customer").fetchall())

        with pytest.raises(InvalidToken):
            rotate_encryption_keys(
                conn,
                old_key=SecretStr(Fernet.generate_key().decode()),  # wrong
                new_key=new_key,
                actor="cli:tester",
            )

        post = dict(
            conn.execute("SELECT id, tax_id_encrypted FROM customer").fetchall()
        )
        assert {k: bytes(v) for k, v in post.items()} == {
            k: bytes(v) for k, v in pre.items()
        }
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM audit_log WHERE action = 'rotate_encryption_key'"
            ).fetchone()[0]
            == 0
        )
    finally:
        conn.close()


def test_delete_customer_hard_rolls_back_when_audit_fails(
    registry_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """LGPD purge atomicity: customer + key rows survive an audit failure."""
    _, kid, cid = seed_customer_and_key(registry_conn)

    import compadre.keys.audit as audit_module

    def _boom(*_args: object, **_kw: object) -> None:
        raise ValueError("forced")

    monkeypatch.setattr(audit_module, "log_event", _boom)

    with pytest.raises(ValueError):
        delete_customer_hard(registry_conn, cid, actor="cli:tester")

    row = registry_conn.execute(
        "SELECT name, deleted_at FROM customer WHERE id = ?", (cid,)
    ).fetchone()
    assert row["name"] == "Helena Ribeiro"
    assert row["deleted_at"] is None
    assert (
        registry_conn.execute("SELECT id FROM api_key WHERE id = ?", (kid,)).fetchone()
        is not None
    )


def test_lookup_key_auth_uses_hash_index(registry_conn: sqlite3.Connection) -> None:
    """Hot path on every POST /v1/audio/transcriptions — must do an indexed seek on
    key_hash, not a table scan. A regression here would quadratically degrade
    auth latency as the registry grows."""
    plaintext, _, _ = seed_customer_and_key(registry_conn)
    key_hash = hash_api_key(plaintext)

    plan = registry_conn.execute(
        "EXPLAIN QUERY PLAN SELECT id, customer_id, status, billing_mode, "
        "rate_limit_rpm, paid_until FROM api_key WHERE key_hash = ? LIMIT 1",
        (key_hash,),
    ).fetchall()
    plan_text = " ".join(str(row["detail"]) for row in plan)
    assert "idx_api_key_hash" in plan_text

    payload = lookup_key_auth(registry_conn, key_hash)
    assert payload is not None and payload.status == "active"
