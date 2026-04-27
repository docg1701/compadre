"""F41 — shared helpers for ``tests/test_keys_cli_*.py`` files.

Single source of truth for the ``CliRunner`` instance, in-memory buffer
helpers, and the seed-by-prefix DB helper (the latter previously
copy-pasted as ``_seed``/``_seed_customer_with_key`` across 8+ files).

Used in concert with the ``env`` and ``freeze_today`` fixtures in
``tests/conftest.py`` and the autouse ``_reset_cli_consoles_to_buffers``
fixture (which swaps the singleton consoles for in-memory buffers).
"""

from __future__ import annotations

import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from typer.testing import CliRunner

from compadre.keys.cli import output as _keys_cli_output
from compadre.keys.cli.app import app as _cli_app
from compadre.keys.db import (
    _apply_migrations,
    generate_customer_id,
    generate_key_id,
    get_connection,
    insert_api_key,
    insert_customer,
)
from compadre.keys.models import ApiKeyRecord, CustomerRecord

runner = CliRunner()
"""Module-level CliRunner — re-used so each test file does not redeclare."""

_DEFAULT_PREFIX = "Pq_abc12"
_DEFAULT_NOW = datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC)


def buffers() -> tuple[str, str]:
    """Return ``(stdout_text, stderr_text)`` from the in-memory consoles."""
    stdout_file = _keys_cli_output._stdout_console.file  # type: ignore[union-attr]
    stderr_file = _keys_cli_output._stderr_console.file  # type: ignore[union-attr]
    return stdout_file.getvalue(), stderr_file.getvalue()


def clear_buffers() -> None:
    """Truncate the stdout/stderr in-memory buffers in place."""
    for attr in ("_stdout_console", "_stderr_console"):
        console = getattr(_keys_cli_output, attr)
        console.file.seek(0)  # type: ignore[union-attr]
        console.file.truncate()  # type: ignore[union-attr]


def seed_by_prefix(
    db_path: str | Path,
    *,
    status: str = "active",
    billing_mode: str = "manual",
    paid_until: str | None = None,
    key_prefix: str = _DEFAULT_PREFIX,
    customer_name: str = "Helena",
    customer_email: str = "h@x.com",
) -> tuple[str, str, str]:
    """Open ``db_path``, migrate, seed one customer + one api_key with a
    known ``key_prefix`` (default ``Pq_abc12``).

    Returns ``(customer_id, key_id, key_prefix)``. The ``key_hash`` is
    a hardcoded ``a*64`` placeholder — CLI tests address keys by
    ``key_prefix`` and never by plaintext.
    """
    conn = get_connection(db_path)
    try:
        _apply_migrations(conn)
        cid = generate_customer_id()
        insert_customer(
            conn,
            CustomerRecord(
                id=cid,
                name=customer_name,
                email=customer_email,
                tax_id_kind="cpf",
                tax_id_encrypted=b"ct",
                notes="",
                registered_at=_DEFAULT_NOW,
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
                key_prefix=key_prefix,
                rate_limit_rpm=60,
                status=status,  # type: ignore[arg-type]
                billing_mode=billing_mode,  # type: ignore[arg-type]
                price_monthly=149.0 if billing_mode == "manual" else 0.0,
                paid_until=paid_until,
                created_at=_DEFAULT_NOW,
                last_used_at=None,
                notes="",
            ),
            actor="cli:tester",
        )
    finally:
        conn.close()
    return cid, kid, key_prefix


def create_customer_via_cli(
    *,
    name: str = "Helena Ribeiro",
    email: str = "helena@clinica.com",
    cpf: str = "123.456.789-09",
    notes: str | None = None,
) -> str:
    """Invoke ``create-customer`` via Typer; return the new customer_id.

    Used by tests that want to exercise the full CLI write path before
    asserting on a sibling verb's behaviour.
    """
    args: list[str] = [
        "create-customer",
        "--name",
        name,
        "--email",
        email,
        "--cpf",
        cpf,
    ]
    if notes is not None:
        args += ["--notes", notes]
    clear_buffers()
    result = runner.invoke(_cli_app, args)
    assert result.exit_code == 0, result.output
    stdout, _ = buffers()
    match = re.search(r"cust_[0-9a-z]{26}", stdout)
    assert match is not None, stdout
    clear_buffers()
    return match.group(0)


def issue_key_via_cli(
    customer_id: str,
    *,
    rpm: int = 60,
    price: float | None = 149.0,
    billing_mode: str = "manual",
) -> None:
    """Invoke ``issue-key`` via Typer; raise on non-zero exit."""
    args = [
        "issue-key",
        "--customer",
        customer_id,
        "--rpm",
        str(rpm),
        "--billing-mode",
        billing_mode,
    ]
    if price is not None and billing_mode == "manual":
        args += ["--price", str(price)]
    clear_buffers()
    result = runner.invoke(_cli_app, args)
    assert result.exit_code == 0, result.output
    clear_buffers()


def latest_key_id_and_prefix(db_path: str | Path, customer_id: str) -> tuple[str, str]:
    """Return ``(key_id, key_prefix)`` of the most recently created key."""
    conn = sqlite3.connect(str(db_path))
    try:
        row: Any = conn.execute(
            "SELECT id, key_prefix FROM api_key WHERE customer_id = ? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 1",
            (customer_id,),
        ).fetchone()
    finally:
        conn.close()
    assert row is not None
    return str(row[0]), str(row[1])
