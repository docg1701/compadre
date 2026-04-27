"""Tests for ``compadre.keys.audit`` — actor + log_event security guards.

Lean by design (test-suite review 2026-04-25): kept the security
contract (forbidden-keys guard catches PII at top level + nested) and
the caller-owned transaction invariant.
"""

from __future__ import annotations

import re
import sqlite3

import pytest

from compadre.keys.audit import filter_forbidden_keys, get_cli_actor, log_event


@pytest.fixture()
def conn() -> sqlite3.Connection:
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute(
        "CREATE TABLE audit_log (id INTEGER PRIMARY KEY AUTOINCREMENT, "
        "occurred_at TEXT NOT NULL, actor TEXT NOT NULL, action TEXT NOT NULL, "
        "target_type TEXT, target_id TEXT, details_json TEXT)"
    )
    c.commit()
    return c


def test_get_cli_actor_always_returns_cli_prefixed_string() -> None:
    """``cli:<user>`` contract; never empty, never 'cli:None'."""
    actor = get_cli_actor()
    assert actor.startswith("cli:")
    assert actor not in {"cli:", "cli:None"}


def test_log_event_writes_iso_utc_row_with_sorted_details(
    conn: sqlite3.Connection,
) -> None:
    """Happy path: ISO-UTC stamp, sorted JSON keys (deterministic for grep
    + diff), all columns wired."""
    log_event(
        conn,
        actor="cli:alice",
        action="create_customer",
        target_type="customer",
        target_id="cust_abc",
        details={"zeta": 1, "alpha": 2, "tax_id_kind": "cpf"},
    )
    row = conn.execute("SELECT * FROM audit_log").fetchone()
    assert (row["actor"], row["action"], row["target_id"]) == (
        "cli:alice",
        "create_customer",
        "cust_abc",
    )
    assert re.match(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$", row["occurred_at"])
    assert row["details_json"] == '{"alpha": 2, "tax_id_kind": "cpf", "zeta": 1}'


@pytest.mark.parametrize(
    "forbidden",
    ["cpf", "CPF", "cnpj", "tax_id", "name", "email", "key_plaintext", "Authorization"],
)
def test_log_event_forbidden_keys_raise_value_error(
    conn: sqlite3.Connection, forbidden: str
) -> None:
    """LGPD: forbidden keys at any nesting level → ValueError. Case-
    insensitive. Uses ValueError (not assert) so guard survives ``-O``."""
    for details in (
        {forbidden: "secret"},
        {"outer": {"inner": {forbidden: "secret"}}},
    ):
        with pytest.raises(ValueError, match="forbidden personal data"):
            log_event(
                conn,
                actor="cli:x",
                action="x",
                target_type=None,
                target_id=None,
                details=details,
            )


def test_log_event_forbidden_key_buried_in_list_of_dicts_raises(
    conn: sqlite3.Connection,
) -> None:
    """FR30 review-pass widening: the write-side guard must walk both
    ``Mapping`` and ``Sequence`` so a forbidden key inside a list of dicts
    is rejected before it lands in the audit log on disk. Symmetric with
    the read-side ``filter_forbidden_keys``."""
    with pytest.raises(ValueError, match="forbidden personal data"):
        log_event(
            conn,
            actor="cli:x",
            action="x",
            target_type=None,
            target_id=None,
            details={"changes": [{"name": "Helena"}]},
        )


def test_filter_forbidden_keys_recurses_into_lists_and_dicts() -> None:
    """Read-side defense-in-depth: forbidden keys nested inside lists,
    lists-of-dicts, and dicts-of-lists are all stripped. The output
    preserves list shape so callers see a faithful structure minus the
    PII keys."""
    payload: dict[str, object] = {
        "items": [{"name": "leak", "ok": 1}, {"email": "x@y", "ok": 2}],
        "nested": {"deeper": [{"cpf": "...", "kind": "cpf"}]},
        "scalar": 42,
        "tax_id": "raw",
    }
    cleaned = filter_forbidden_keys(payload)
    assert cleaned == {
        "items": [{"ok": 1}, {"ok": 2}],
        "nested": {"deeper": [{"kind": "cpf"}]},
        "scalar": 42,
    }
    # Source dict is not mutated.
    assert "tax_id" in payload


def test_log_event_caller_owns_txn_and_none_details_writes_sql_null(
    conn: sqlite3.Connection,
) -> None:
    """log_event MUST NOT commit (callers compose inside ``with conn:``);
    ``details=None`` must land as SQL NULL, not the string ``"null"``."""
    conn.isolation_level = "DEFERRED"
    log_event(
        conn,
        actor="cli:x",
        action="x",
        target_type=None,
        target_id=None,
        details=None,
    )
    # details=None landed as NULL.
    row = conn.execute("SELECT details_json FROM audit_log").fetchone()
    assert row["details_json"] is None
    # Caller didn't commit — rollback nukes the row.
    conn.rollback()
    assert conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0] == 0
