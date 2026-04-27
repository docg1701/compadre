"""`compadre-keys create-customer` (and future customer subcommands).

Exit-code contract (UX-DR10):
  0 — success
  2 — usage error (Typer rejects / mutex violation / bad email /
      --hard missing / --format invalid / confirm-abort)
  3 — not-found (tombstoned or missing customer on show / update-customer /
      export-customer / delete-customer)
  4 — validation error (tax-ID checksum)
  5 — database error (sqlite3.Error / fernet InvalidToken on export
      decrypt — the only path where decrypt can fail operationally)
  6 — permission / configuration error (missing encryption key, etc.)
"""

from __future__ import annotations

import contextlib
import json
import re
import sqlite3
import sys
from datetime import UTC, datetime
from typing import cast

import click
import typer
from cryptography.fernet import InvalidToken
from pydantic import EmailStr, TypeAdapter, ValidationError

from compadre.config import get_registry_settings
from compadre.counters import aggregate_key_stats, read_all_snapshots
from compadre.exceptions import InvalidTaxIdError
from compadre.keys.audit import filter_forbidden_keys, get_cli_actor
from compadre.keys.cli import _clock
from compadre.keys.cli._errors import first_error_msg, is_missing_encryption_key
from compadre.keys.cli.commands.keys import load_settings_and_tz
from compadre.keys.cli.output import (
    EXPORT_FORMAT_CHOICES,
    LIST_FORMAT_CHOICES,
    render_audit_tail,
    render_confirmation,
    render_customer_detail,
    render_customer_list,
    render_empty_state,
    render_error,
    render_export_json,
    render_export_yaml,
    render_json,
    render_key_detail,
    render_last_24h_stats,
    render_show_json,
    stderr_console,
)
from compadre.keys.crypto import (
    compute_tax_id_lookup,
    decrypt_tax_id,
    encrypt_tax_id,
    strip_tax_id,
    validate_cnpj,
    validate_cpf,
)
from compadre.keys.db import (
    CorrectTaxIdResult,
    UpdateCustomerResult,
    _apply_migrations,
    backfill_tax_id_lookup_if_needed,
    correct_customer_tax_id,
    count_tombstones,
    delete_customer_hard,
    generate_customer_id,
    get_connection,
    get_customer,
    get_customer_with_cipher,
    insert_customer,
    is_first_run,
    list_api_keys,
    list_audit_events,
    list_customers,
    resolve_show_target,
    update_customer,
)
from compadre.keys.formatters import format_tax_id_display
from compadre.keys.models import (
    AuditEventRow,
    CustomerDetailRow,
    CustomerListRow,
    CustomerRecord,
    KeyDetailRow,
    Last24hStats,
    TaxIdKind,
)
from compadre.logging import get_logger

_logger = get_logger("keys.cli")

app = typer.Typer(help="Customer registry commands.", no_args_is_help=True)

_missing_key_hint = (
    "Generate one with: python -c 'from cryptography.fernet import "
    "Fernet; print(Fernet.generate_key().decode())' and add it to "
    ".env. Restart the service if monetization is enabled there too."
)


@app.command("create-customer")
def create_customer(
    name: str = typer.Option(
        ...,
        "--name",
        help='Customer full name. Example: "Helena Ribeiro".',
    ),
    email: str = typer.Option(
        ...,
        "--email",
        help='Customer email. Example: "helena@clinica.com".',
    ),
    cpf: str | None = typer.Option(
        None,
        "--cpf",
        help=(
            'CPF (with or without formatting). Example: "123.456.789-09". '
            "Mutually exclusive with --cnpj."
        ),
    ),
    cnpj: str | None = typer.Option(
        None,
        "--cnpj",
        help=('CNPJ. Example: "12.345.678/0001-95". Mutually exclusive with --cpf.'),
    ),
    notes: str = typer.Option(
        "",
        "--notes",
        help='Free-form notes. Example: "5 bots, WhatsApp booking".',
    ),
) -> None:
    """Register a new customer with CPF/CNPJ validation and at-rest encryption."""
    # 0. Reject NUL / control bytes in --name / --email and whitespace-only
    # --name. CustomerCreate's pydantic validators catch many shape errors
    # but accept embedded NUL bytes that SQLite stores but later greps and
    # log aggregators mishandle. Whitespace-only names pass min_length but
    # produce an opaque "Customer  " identity in audit logs.
    if any(ord(ch) < 0x20 and ch != "\t" for ch in name):
        render_error(
            "Usage error: --name contains illegal control characters",
            "Provide --name without NUL, newline, or ANSI-escape bytes.",
        )
        raise typer.Exit(code=2)
    # B3 — cap --notes at the same length the ``update-customer --notes``
    # path enforces, so the operator never gets into a state where a
    # ``create-customer`` succeeded but the next ``update-customer
    # --notes`` (replace mode) would silently fail or truncate. Defense-
    # in-depth: SQLite has no native TEXT length cap, so a runaway
    # ``--notes`` would otherwise grow the registry row unbounded.
    if len(notes) > _UPDATE_CUSTOMER_NOTES_REPLACE_MAX:
        render_error(
            f"Usage error: --notes exceeds {_UPDATE_CUSTOMER_NOTES_REPLACE_MAX} "
            "characters",
            f"Trim --notes to {_UPDATE_CUSTOMER_NOTES_REPLACE_MAX} chars or fewer.",
        )
        raise typer.Exit(code=2)
    if not name.strip():
        render_error(
            "Usage error: --name must not be empty or whitespace-only",
            "Provide a non-empty --name.",
        )
        raise typer.Exit(code=2)
    if any(ord(ch) < 0x20 and ch != "\t" for ch in email):
        render_error(
            "Usage error: --email contains illegal control characters",
            "Provide --email without NUL, newline, or ANSI-escape bytes.",
        )
        raise typer.Exit(code=2)

    # 1. Mutex check for --cpf / --cnpj.
    if (cpf is None) == (cnpj is None):
        if cpf is not None:
            render_error(
                "Usage error: --cpf and --cnpj are mutually exclusive",
                "Provide exactly one of --cpf or --cnpj.",
            )
        else:
            render_error(
                "Usage error: exactly one of --cpf or --cnpj is required",
                "Provide exactly one of --cpf or --cnpj.",
            )
        raise typer.Exit(code=2)

    # 2. Load settings (trap missing/malformed encryption key or unreadable .env).
    try:
        settings = get_registry_settings()
    except ValidationError as exc:
        if is_missing_encryption_key(exc):
            render_error(
                "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
                _missing_key_hint,
            )
        else:
            render_error("Configuration error", first_error_msg(exc))
        raise typer.Exit(code=6) from None
    except (OSError, PermissionError) as exc:
        render_error("Configuration error: settings file unreadable", str(exc))
        raise typer.Exit(code=6) from None

    if settings.key_registry_encryption_key is None:
        render_error(
            "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
            _missing_key_hint,
        )
        raise typer.Exit(code=6)

    # 3. Determine kind + digits + validate checksum.
    if cpf is not None:
        kind: TaxIdKind = "cpf"
        digits = strip_tax_id(cpf)
    else:
        kind = "cnpj"
        digits = strip_tax_id(cast(str, cnpj))

    try:
        if kind == "cpf":
            validate_cpf(digits)
        else:
            validate_cnpj(digits)
    except InvalidTaxIdError as exc:
        render_error(exc.message, exc.hint)
        raise typer.Exit(code=2) from None

    # 4. Validate name/email by constructing the pydantic model.
    from compadre.keys.models import CustomerCreate

    try:
        CustomerCreate(
            name=name,
            email=email,
            tax_id_kind=kind,
            tax_id_digits=digits,
            notes=notes,
        )
    except ValidationError as exc:
        render_error(
            "Usage error: invalid customer attributes",
            first_error_msg(exc),
        )
        raise typer.Exit(code=2) from None

    # 5. Encrypt tax ID + compute the deterministic lookup digest (B2 —
    # the partial UNIQUE INDEX on tax_id_lookup detects duplicates at the
    # DB boundary; same-key HMAC means rotate-encryption-key recomputes
    # both columns atomically).
    encrypted = encrypt_tax_id(digits, settings.key_registry_encryption_key)
    lookup = compute_tax_id_lookup(digits, settings.key_registry_encryption_key)

    # 6. Open DB, apply migrations, insert customer + audit in one txn.
    record = CustomerRecord(
        id=generate_customer_id(),
        name=name,
        email=email,
        tax_id_kind=kind,
        tax_id_encrypted=encrypted,
        notes=notes,
        registered_at=datetime.now(UTC).replace(microsecond=0),
        deleted_at=None,
    )

    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from None
    try:
        try:
            _apply_migrations(conn)
            backfill_tax_id_lookup_if_needed(
                conn, encryption_key=settings.key_registry_encryption_key
            )
            insert_customer(conn, record, actor=get_cli_actor(), tax_id_lookup=lookup)
        except sqlite3.IntegrityError as exc:
            # B2: partial UNIQUE INDEX rejected the insert because an
            # active row already holds the same tax-ID. Translate to
            # the user-facing ``DuplicateTaxIdError`` shape — the raw
            # value never appears in the error chain (anti-enumeration).
            if "tax_id_lookup" not in str(exc):
                render_error("Database error", str(exc))
                raise typer.Exit(code=5) from None
            from compadre.exceptions import DuplicateTaxIdError

            err = DuplicateTaxIdError(kind)
            render_error(err.message, err.hint)
            raise typer.Exit(code=2) from None
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    _logger.info(
        "Customer created | id=%s | tax_id_kind=%s", record.id, record.tax_id_kind
    )
    render_confirmation(f"Customer created: {record.id} ({record.name})")


_FIRST_RUN_CUSTOMER_HINT = (
    'To get started: compadre-keys create-customer --name "Name" '
    '--email "email@example.com" --cpf "000.000.000-00"'
)


def _customer_row_to_json(row: CustomerListRow) -> dict[str, object]:
    """Serialize a ``CustomerListRow`` to the AC4 JSON shape (fixed key order)."""
    return {
        "id": row.id,
        "name": row.name,
        "email": row.email,
        "tax_id_kind": row.tax_id_kind,
        "key_count": row.key_count,
        "registered_at": row.registered_at,
        "deleted_at": row.deleted_at,
        "notes": row.notes,
    }


@app.command("list-customers")
def list_customers_cmd(
    deleted: bool = typer.Option(
        False,
        "--deleted",
        help="Include purged customers (tombstoned by LGPD hard-delete).",
    ),
    format: str = typer.Option(
        "table",
        "--format",
        help=(
            "Output format: table (default, human-readable) or json. "
            "Example: --format json."
        ),
    ),
) -> None:
    """List registered customers with key counts and registration dates.

    Example: compadre-keys list-customers --deleted --format json
    """
    # 1. Validate --format upfront. Accepting unknown formats would silently
    # fall through to the table path; explicit rejection is UX-DR10.
    if format not in LIST_FORMAT_CHOICES:
        render_error(
            "Usage error: --format must be table or json",
            "Valid values: table, json.",
        )
        raise typer.Exit(code=2)

    # 2. Load settings — same exit-6 mapping as create-customer / issue-key.
    try:
        settings = get_registry_settings()
    except ValidationError as exc:
        if is_missing_encryption_key(exc):
            render_error(
                "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
                _missing_key_hint,
            )
        else:
            render_error("Configuration error", first_error_msg(exc))
        raise typer.Exit(code=6) from None
    except (OSError, PermissionError) as exc:
        render_error("Configuration error: settings file unreadable", str(exc))
        raise typer.Exit(code=6) from None

    if settings.key_registry_encryption_key is None:
        render_error(
            "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
            _missing_key_hint,
        )
        raise typer.Exit(code=6)

    # 3. Open DB + apply migrations + read rows. Close in finally + suppress
    # (listings are pure reads — close errors here have nothing to protect).
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from None

    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        rows = list_customers(conn, include_deleted=deleted)
        first_run = is_first_run(conn) if not rows else False
        tombstone_total = count_tombstones(conn) if not deleted and not rows else 0
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    _logger.info("Listed customers | count=%d | include_deleted=%s", len(rows), deleted)

    # 4. Branch on --format. JSON path never emits hint text (AC4).
    if format == "json":
        render_json([_customer_row_to_json(r) for r in rows])
        return

    title = "Customers (including purged)" if deleted else "Customers"

    if rows:
        render_customer_list(title, rows)
        return

    # Empty branches, ordered by UX-DR13 priority.
    if first_run:
        render_empty_state(["No customers registered yet.", _FIRST_RUN_CUSTOMER_HINT])
        return

    if not deleted and tombstone_total > 0:
        render_empty_state(
            [
                "No active customers. Run 'compadre-keys list-customers "
                "--deleted' to see purged records."
            ]
        )
        return

    render_empty_state(["No customers found."])


# ---------------------------------------------------------------------------
# `show` — single-target detail view with optional audit tail (Story 1.4).
# ---------------------------------------------------------------------------

_SHOW_NOT_FOUND_HINT = (
    "Run 'compadre-keys list-customers' or 'compadre-keys list-keys' to see "
    "registered records."
)

_SHOW_AMBIGUOUS_HINT = (
    "Run 'compadre-keys list-keys' to see the full list, then use the key's "
    "customer id (cust_...) instead."
)


def _customer_detail_to_json(row: CustomerDetailRow) -> dict[str, object]:
    """Serialize a ``CustomerDetailRow`` to the AC8 JSON shape (fixed key order)."""
    return {
        "id": row.id,
        "name": row.name,
        "email": row.email,
        "tax_id_kind": row.tax_id_kind,
        "key_count": row.key_count,
        "registered_at": row.registered_at,
        "notes": row.notes,
    }


def _key_detail_to_json(row: KeyDetailRow) -> dict[str, object]:
    """Serialize a ``KeyDetailRow`` to the AC8 JSON shape (fixed key order)."""
    return {
        "id": row.id,
        "customer_id": row.customer_id,
        "customer_name": row.customer_name,
        "key_prefix": row.key_prefix,
        "status": row.status,
        "billing_mode": row.billing_mode,
        "rate_limit_rpm": row.rate_limit_rpm,
        "price_monthly": row.price_monthly,
        "paid_until": row.paid_until,
        "created_at": row.created_at,
        "last_used_at": row.last_used_at,
        "age_days": row.age_days,
        "notes": row.notes,
    }


def _load_last_24h_stats(
    *,
    key_id: str,
    db_path: str,
) -> Last24hStats | None:
    """Aggregate per-replica counters snapshots for ``key_id``, or return ``None``.

    ``None`` means no replica's snapshot contains this ``key_id`` — either
    because the API isn't running on any replica, or because the key has
    not served a request since the last process restart on any replica
    that is reporting. Every non-fatal parse surprise degrades to
    "replica not contributing" with a WARN log; ``show`` never crashes
    on a weird snapshot.

    Data is surfaced regardless of snapshot age. The renderer labels the
    freshness (``snapshot_age_seconds``) so the operator can judge; we
    don't hide data behind an arbitrary 60-second cutoff — the "last-24h"
    name already implies the label is about aggregation, not freshness.
    """
    snapshots = read_all_snapshots(db_path)
    aggregated = aggregate_key_stats(snapshots, key_id)
    if aggregated is None:
        return None
    return Last24hStats(
        requests_total=aggregated.requests_total,
        auth_success=aggregated.auth_success,
        rate_limit_429=aggregated.rate_limit_429,
        peak_rpm=aggregated.peak_rpm,
        avg_rpm_last_60s=aggregated.avg_rpm_last_60s,
        last_activity_at=aggregated.last_activity_at,
        snapshot_age_seconds=aggregated.snapshot_age_seconds,
        process_uptime_seconds=aggregated.process_uptime_seconds,
        replicas=aggregated.replicas,
    )


def _last_24h_stats_to_json(stats: Last24hStats) -> dict[str, object]:
    """Serialize ``Last24hStats`` to the ``show --format json`` ``stats`` shape."""
    return {
        "requests_total": stats.requests_total,
        "auth_success": stats.auth_success,
        "rate_limit_429": stats.rate_limit_429,
        "peak_rpm": stats.peak_rpm,
        "avg_rpm_last_60s": stats.avg_rpm_last_60s,
        "last_activity_at": stats.last_activity_at,
        "snapshot_age_seconds": stats.snapshot_age_seconds,
        "process_uptime_seconds": stats.process_uptime_seconds,
        "replicas": stats.replicas,
    }


def _audit_event_to_json(event: AuditEventRow) -> dict[str, object]:
    """Serialize an ``AuditEventRow`` to the AC8 event shape (fixed key order).

    ``details_json`` is re-parsed into a dict/list for consumer ergonomics —
    callers shouldn't have to double-decode the nested payload. Malformed
    rows (shouldn't happen in practice — every writer in the codebase uses
    ``json.dumps`` with ``sort_keys=True``) fall back to the raw string.
    """
    if event.details_json is None:
        details: object | None = None
    else:
        try:
            details = json.loads(event.details_json)
        except json.JSONDecodeError:
            details = event.details_json
    return {
        "id": event.id,
        "occurred_at": event.occurred_at,
        "actor": event.actor,
        "action": event.action,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "details": details,
    }


@app.command("show")
def show_cmd(
    target: str = typer.Argument(
        ...,
        metavar="TARGET",
        help=(
            "Customer ID (cust_...) or 8-char key prefix. "
            'Example: "EVb4xk8B" or "cust_01j8xr3e4c5m7kn9p2qt8vwy".'
        ),
    ),
    audit_tail: int | None = typer.Option(
        None,
        "--audit-tail",
        help=(
            "Append the N most recent audit events. "
            "Example: --audit-tail 20. Range: 1..1000."
        ),
    ),
    format: str = typer.Option(
        "table",
        "--format",
        help=(
            "Output format: table (default, human-readable) or json. "
            "Example: --format json."
        ),
    ),
) -> None:
    """Show full context for a customer or API key (optionally with audit tail).

    Example: compadre-keys show EVb4xk8B --audit-tail 20
    """
    # 1. Literal flag validation.
    if format not in LIST_FORMAT_CHOICES:
        render_error(
            "Usage error: --format must be table or json",
            "Valid values: table, json.",
        )
        raise typer.Exit(code=2)
    if audit_tail is not None and not 1 <= audit_tail <= 1000:
        render_error(
            "Usage error: --audit-tail must be between 1 and 1000",
            "Provide a positive integer in the range 1..1000.",
        )
        raise typer.Exit(code=2)

    # 2. Settings + encryption-key sanity (F38 — adopt shared ladder).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded

    today = _clock.resolve_today_in_tz(billing_tz)

    # 3. Open DB + migrations + resolve target + fetch events.
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from None

    result_kind: str = "not_found"
    events: list[AuditEventRow] | None = None
    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        resolved = resolve_show_target(conn, target, today=today)
        result_kind = resolved.kind

        if resolved.kind == "not_found":
            render_error(
                f"Not found: no customer or key matches '{target}'",
                _SHOW_NOT_FOUND_HINT,
            )
            raise typer.Exit(code=3)
        if resolved.kind == "key_ambiguous":
            render_error(
                f"Ambiguous prefix: multiple keys match '{target}'",
                _SHOW_AMBIGUOUS_HINT,
            )
            raise typer.Exit(code=3)

        if audit_tail is not None:
            if resolved.kind == "customer":
                customer_row = cast(CustomerDetailRow, resolved.value)
                key_id_rows = conn.execute(
                    "SELECT id FROM api_key WHERE customer_id = ?",
                    (customer_row.id,),
                ).fetchall()
                target_ids = [customer_row.id, *[r["id"] for r in key_id_rows]]
            else:
                key_row = cast(KeyDetailRow, resolved.value)
                target_ids = [key_row.id]
            events = list_audit_events(conn, target_ids, limit=audit_tail)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # Story 2.3 — per-key last-24h stats. Key-target-only (AC14); the
    # customer-target aggregate is future work. Stats are only loaded when
    # ``--audit-tail`` is requested because that is the forensic surface —
    # a bare ``show`` stays snappy and untouched by file I/O on the
    # snapshot.
    stats: Last24hStats | None = None
    if audit_tail is not None and resolved.kind == "key_single":
        key_row = cast(KeyDetailRow, resolved.value)
        stats = _load_last_24h_stats(
            key_id=key_row.id,
            db_path=str(settings.key_registry_db_path),
        )

    # Redact pasted-plaintext targets before logging — the resolver accepts
    # `cmpd_live_<secret>` as a convenience for operators who paste the full
    # key, but the secret must never reach the structured log. Truncating to
    # the first 8 chars of the suffix mirrors the resolver's own lookup key.
    if target.startswith("cmpd_live_"):
        target_log = "cmpd_live_" + target.removeprefix("cmpd_live_")[:8] + "…"
    else:
        target_log = target
    _logger.info(
        "Showed detail | kind=%s | target=%s | audit_tail=%s",
        result_kind,
        target_log,
        audit_tail,
    )

    # 4. Render branch.
    events_payload: list[dict[str, object]] | None
    if events is None:
        events_payload = None
    else:
        events_payload = [_audit_event_to_json(e) for e in events]

    if format == "json":
        if resolved.kind == "customer":
            render_show_json(
                target_type="customer",
                target=_customer_detail_to_json(
                    cast(CustomerDetailRow, resolved.value)
                ),
                events=events_payload,
            )
        else:
            render_show_json(
                target_type="api_key",
                target=_key_detail_to_json(cast(KeyDetailRow, resolved.value)),
                events=events_payload,
                stats=_last_24h_stats_to_json(stats) if stats is not None else None,
                include_stats=audit_tail is not None,
            )
        return

    if resolved.kind == "customer":
        render_customer_detail(cast(CustomerDetailRow, resolved.value))
    else:
        render_key_detail(cast(KeyDetailRow, resolved.value))
        # Story 2.3 — last-24h stats between DetailPanel and AuditTail.
        # Rendered only when ``--audit-tail`` was requested (the operator
        # is already in "forensic mode"); a bare ``show <key>`` stays
        # exactly as Story 1.4 shipped it.
        if audit_tail is not None:
            render_last_24h_stats(stats)

    if events is not None:
        render_audit_tail(events)


# ---------------------------------------------------------------------------
# `update-customer` — email + append-note mutation (Story 1.5).
# ---------------------------------------------------------------------------

_EMAIL_ADAPTER = TypeAdapter(EmailStr)

_UPDATE_CUSTOMER_NEITHER_HINT = (
    "Provide at least one of --name, --email, --add-note, or --notes."
)
_UPDATE_CUSTOMER_NOTES_MUTEX_HINT = (
    "Use either --add-note (append a dated entry) OR --notes (replace the "
    "whole notes column), not both."
)
_UPDATE_CUSTOMER_NOTE_EMPTY_HINT = (
    "Provide note text with content. Whitespace-only notes are rejected."
)
_UPDATE_CUSTOMER_NOTE_NUL_HINT = "Note text must not contain NUL bytes."
_UPDATE_CUSTOMER_NOTE_NEWLINE_HINT = (
    "Note text must be a single line. Newlines would forge dated entries in "
    "the stored notes column."
)
_UPDATE_CUSTOMER_NOT_FOUND_HINT = (
    "Run 'compadre-keys list-customers' to see registered records."
)
_UPDATE_CUSTOMER_NAME_MAX = 200
_UPDATE_CUSTOMER_NOTE_MAX = 1024
# Replace-mode allows newlines (operators consolidate dated entries) so the
# cap is generous compared to the per-line --add-note ceiling. Bounded so a
# stray paste-the-whole-logfile mistake stays catchable.
_UPDATE_CUSTOMER_NOTES_REPLACE_MAX = 8192


def _update_confirmation_suffix(
    updated_fields: tuple[str, ...],
    notes_op: str | None = None,
) -> str:
    """Map ``updated_fields`` to the ConfirmationLine human suffix.

    Grammar is fixed: ``email changed`` / ``name changed`` /
    ``note added`` / ``notes replaced`` joined with ``, `` (comma-space)
    when multiple fire. Story 1.5 AC1/AC2/AC3 + Story 5.1 Gaps 1/2.
    """
    parts: list[str] = []
    if "name" in updated_fields:
        parts.append("name changed")
    if "email" in updated_fields:
        parts.append("email changed")
    if "notes" in updated_fields:
        parts.append("notes replaced" if notes_op == "replace" else "note added")
    return ", ".join(parts)


@app.command("update-customer")
def update_customer_cmd(
    customer_id: str = typer.Argument(
        ...,
        metavar="CUSTOMER_ID",
        help=(
            "Customer ID from create-customer / list-customers. "
            "Example: cust_01j8xr3e4c5m7kn9p2qt8vwy."
        ),
    ),
    name: str | None = typer.Option(
        None,
        "--name",
        help=(
            'Replace the stored display name. Example: --name "Helena Ribeiro Silva".'
        ),
    ),
    email: str | None = typer.Option(
        None,
        "--email",
        help=(
            "Replace the stored email address. "
            'Example: --email "helena@novaclinica.com".'
        ),
    ),
    add_note: str | None = typer.Option(
        None,
        "--add-note",
        help=(
            "Append a dated note to the customer record. "
            'Example: --add-note "paga tarde - lembrar dia 3".'
        ),
    ),
    notes: str | None = typer.Option(
        None,
        "--notes",
        help=(
            "Replace the entire notes column (multi-line allowed). "
            'Mutually exclusive with --add-note. Example: --notes "rebuilt '
            'history\\n[2026-04-25] consolidated".'
        ),
    ),
) -> None:
    """Update a customer's name, email, and/or notes (append OR replace).

    Example: compadre-keys update-customer cust_01j8xr3e4c5m7kn9p2qt8vwy
             --add-note "paga tarde - lembrar dia 3"
    """
    # 1. Neither-flag check — runs before any DB / settings work so the
    # cheapest usage mistake never opens a connection.
    if name is None and email is None and add_note is None and notes is None:
        render_error(
            "Usage error: at least one of --name, --email, --add-note, "
            "or --notes is required",
            _UPDATE_CUSTOMER_NEITHER_HINT,
        )
        raise typer.Exit(code=2)

    # 1b. Mutual exclusion: --add-note (append) vs --notes (replace).
    if add_note is not None and notes is not None:
        render_error(
            "Usage error: --add-note and --notes are mutually exclusive",
            _UPDATE_CUSTOMER_NOTES_MUTEX_HINT,
        )
        raise typer.Exit(code=2)

    # 2. Name shape check (if provided). Mirrors create-customer's
    # control-byte / whitespace / length pipeline.
    if name is not None:
        if any(ord(ch) < 0x20 and ch != "\t" for ch in name):
            render_error(
                "Usage error: --name contains illegal control characters",
                "Provide --name without NUL, newline, or ANSI-escape bytes.",
            )
            raise typer.Exit(code=2)
        stripped_name = name.strip()
        if not stripped_name:
            render_error(
                "Usage error: --name must not be empty or whitespace-only",
                "Provide a non-empty --name.",
            )
            raise typer.Exit(code=2)
        if len(stripped_name) > _UPDATE_CUSTOMER_NAME_MAX:
            render_error(
                f"Usage error: --name exceeds {_UPDATE_CUSTOMER_NAME_MAX} characters",
                f"Trim --name to {_UPDATE_CUSTOMER_NAME_MAX} chars or fewer.",
            )
            raise typer.Exit(code=2)
        name = stripped_name

    # 3. Email shape check (if provided). Pre-strip before pydantic so
    # operators who copy-paste with trailing whitespace get the same
    # normalized storage as other commands.
    if email is not None:
        stripped_email = email.strip()
        try:
            email = _EMAIL_ADAPTER.validate_python(stripped_email)
        except ValidationError as exc:
            render_error("Usage error: invalid email", first_error_msg(exc))
            raise typer.Exit(code=2) from None

    # 4. Note shape check (if provided). Strip outer whitespace, reject
    # empty-after-strip, and reject NUL bytes (Story 1.1 deferred UX edge
    # picked up here — SQLite accepts NULs but they break later greps).
    if add_note is not None:
        if "\x00" in add_note:
            render_error(
                "Usage error: --add-note contains illegal characters",
                _UPDATE_CUSTOMER_NOTE_NUL_HINT,
            )
            raise typer.Exit(code=2)
        # Reject embedded newlines. The stored format is
        # ``[YYYY-MM-DD] text`` joined with ``\n`` separators, so any LF/CR
        # inside operator-supplied text would be indistinguishable from a
        # real stamped entry on later reads — a forgery vector.
        if "\n" in add_note or "\r" in add_note:
            render_error(
                "Usage error: --add-note must be a single line",
                _UPDATE_CUSTOMER_NOTE_NEWLINE_HINT,
            )
            raise typer.Exit(code=2)
        # Reject ANSI escape (\x1b) and other C0 controls except TAB. They
        # forge terminal formatting on later render via Rich/journalctl/etc.
        if any(ord(ch) < 0x20 and ch not in ("\t", "\n", "\r") for ch in add_note):
            render_error(
                "Usage error: --add-note contains illegal control characters",
                (
                    "Note text must not contain ANSI escape (\\x1b) or "
                    "other C0 control bytes."
                ),
            )
            raise typer.Exit(code=2)
        stripped_note = add_note.strip()
        if not stripped_note:
            render_error(
                "Usage error: --add-note must be non-empty",
                _UPDATE_CUSTOMER_NOTE_EMPTY_HINT,
            )
            raise typer.Exit(code=2)
        if len(stripped_note) > _UPDATE_CUSTOMER_NOTE_MAX:
            render_error(
                f"Usage error: --add-note exceeds {_UPDATE_CUSTOMER_NOTE_MAX} "
                "characters",
                f"Trim --add-note to {_UPDATE_CUSTOMER_NOTE_MAX} chars or fewer.",
            )
            raise typer.Exit(code=2)
        add_note = stripped_note

    # 4b. Replace-mode notes shape check. Newlines are allowed (operators
    # consolidate dated entries here) but NUL / C0-controls (except
    # tab/CR/LF) are still rejected — same anti-forgery / anti-ANSI posture.
    if notes is not None:
        if not notes.strip():
            # Empty / whitespace-only --notes would silently wipe the column;
            # an operator who types --notes "" almost certainly wants a no-op.
            # Refuse explicitly — actual erasure is delete-customer --hard.
            render_error(
                "Usage error: --notes must not be empty or whitespace-only",
                "Provide --notes content. Whitespace-only would silently "
                "wipe the column.",
            )
            raise typer.Exit(code=2)
        if "\x00" in notes:
            render_error(
                "Usage error: --notes contains illegal characters",
                _UPDATE_CUSTOMER_NOTE_NUL_HINT,
            )
            raise typer.Exit(code=2)
        if any(ord(ch) < 0x20 and ch not in ("\t", "\n", "\r") for ch in notes):
            render_error(
                "Usage error: --notes contains illegal control characters",
                (
                    "Notes must not contain ANSI escape (\\x1b) or other "
                    "C0 control bytes (newlines / tabs are allowed)."
                ),
            )
            raise typer.Exit(code=2)
        if len(notes) > _UPDATE_CUSTOMER_NOTES_REPLACE_MAX:
            render_error(
                f"Usage error: --notes exceeds "
                f"{_UPDATE_CUSTOMER_NOTES_REPLACE_MAX} characters",
                f"Trim --notes to {_UPDATE_CUSTOMER_NOTES_REPLACE_MAX} chars or fewer.",
            )
            raise typer.Exit(code=2)

    # 4. Settings load (F38 — adopt shared ladder).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded

    today = _clock.resolve_today_in_tz(billing_tz)

    # 5. Open DB + migrations + mutation. Close in finally + suppress.
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from None

    result: UpdateCustomerResult | None
    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        try:
            result = update_customer(
                conn,
                customer_id,
                new_email=email,
                add_note=add_note,
                new_name=name,
                replace_notes=notes,
                actor=get_cli_actor(),
                today=today,
            )
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            # ``update_customer`` raises ``RuntimeError`` when the post-commit
            # re-read returns ``None`` (a concurrent tombstone between commit
            # and re-read — "shouldn't happen" in the single-writer CLI);
            # ``sqlite3.Error`` covers the SQL-time failures; ``OSError``
            # the DB-file-unreachable path. All three map to exit 5.
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    if result is None:
        render_error(
            f"Customer not found: {customer_id}",
            _UPDATE_CUSTOMER_NOT_FOUND_HINT,
        )
        raise typer.Exit(code=3)

    if not result.updated_fields:
        # B7 \u2014 same-value writes intentionally produce no UPDATE and no
        # audit row (CLAUDE.md "no-op write" contract). Surface that
        # honestly to the operator instead of "Updated: ... \u2014 " with
        # an empty suffix that implies a mutation happened.
        _logger.info("Updated customer (no-op) | id=%s | fields=", customer_id)
        render_confirmation(
            f"No changes: {result.customer.id} ({result.customer.name}) "
            "\u2014 already up to date"
        )
        return

    _logger.info(
        "Updated customer | id=%s | fields=%s",
        customer_id,
        ",".join(result.updated_fields),
    )
    suffix = _update_confirmation_suffix(result.updated_fields, result.notes_op)
    render_confirmation(
        f"Updated: {result.customer.id} ({result.customer.name}) \u2014 {suffix}"
    )


# ---------------------------------------------------------------------------
# `export-customer` — LGPD Art. 18 II/V machine-readable export (Story 4.1).
# ---------------------------------------------------------------------------

_CUSTOMER_ID_PATTERN = re.compile(r"\Acust_[0-9a-z]{26}\Z")

_EXPORT_NOT_FOUND_HINT = "Run 'compadre-keys list-customers' to see registered records."
_EXPORT_BAD_ID_HINT = (
    "Customer IDs have the shape cust_<26-char-ULID>. "
    "Use list-customers to find the right id."
)
_EXPORT_FORMAT_HINT = "Valid values: json, yaml."
_EXPORT_DECRYPT_HINT = (
    "Check KEY_REGISTRY_ENCRYPTION_KEY matches the deployment-time key. "
    "If the key has changed, restore the previous value or run "
    "rotate-encryption-key (Story 4.2)."
)


_EXPORT_AUDIT_LIMIT = 1000  # matches list_audit_events upper bound


def _audit_event_to_export_dict(event: AuditEventRow) -> dict[str, object]:
    """Project an ``AuditEventRow`` to the export envelope shape.

    Always emits ``action``, ``actor``, ``occurred_at``. Emits ``details``
    only when the source row carried a non-empty payload (post forbidden-key
    filter). The filter is defense-in-depth — ``log_event`` already enforces
    the same blocklist at write time. Corrupt rows (non-decodable JSON or
    JSON whose top-level is not an object) emit ``details: {"_corrupt": true}``
    so the export honestly signals that something was elided rather than
    silently dropping the field."""
    out: dict[str, object] = {
        "action": event.action,
        "actor": event.actor,
        "occurred_at": event.occurred_at,
    }
    if event.details_json:
        try:
            raw = json.loads(event.details_json)
        except json.JSONDecodeError:
            out["details"] = {"_corrupt": True}
            return out
        if isinstance(raw, dict):
            cleaned = filter_forbidden_keys(raw)
            if cleaned:
                out["details"] = cleaned
        else:
            # Non-dict top-level (list / string / number) — out-of-band
            # insertion path; surface a sentinel rather than dropping
            # silently. The forbidden-key filter is dict-shaped, so we
            # cannot scrub a non-dict payload safely; mark and move on.
            out["details"] = {"_corrupt": True}
    return out


@app.command("export-customer")
def export_customer_cmd(
    customer_id: str = typer.Argument(
        ...,
        metavar="CUSTOMER_ID",
        help=(
            "Customer ID to export (from create-customer / list-customers). "
            "Example: cust_01j8xr3e4c5m7kn9p2qt8vwy."
        ),
    ),
    format: str = typer.Option(
        "json",
        "--format",
        help=(
            "Output format: json (default, machine-readable) or yaml "
            "(human-readable for data-subject access response). "
            "Example: --format yaml."
        ),
    ),
    include_audit: bool = typer.Option(
        False,
        "--include-audit",
        help=(
            "Append the customer's audit history to the export envelope under "
            "``audit_events``. Default off — the LGPD minimum-necessary "
            "reading is the lean export. Opt in when legal counsel reads "
            "Art. 18 II/V broadly. Story 5.1 Gap 6."
        ),
    ),
) -> None:
    """Export a customer's stored data to stdout for LGPD Art. 18 II/V response.

    Example: compadre-keys export-customer cust_01j8xr3e4c5m7kn9p2qt8vwy
             compadre-keys export-customer cust_01j8xr3e4c5m7kn9p2qt8vwy --format yaml
    """
    # 1. Literal flag validation.
    if format not in EXPORT_FORMAT_CHOICES:
        render_error(
            "Usage error: --format must be json or yaml",
            _EXPORT_FORMAT_HINT,
        )
        raise typer.Exit(code=2)

    # 2. Customer ID shape validation — pre-DB so typos / pasted secrets
    # never open a connection.
    if not _CUSTOMER_ID_PATTERN.match(customer_id):
        render_error(
            "Usage error: invalid customer id",
            _EXPORT_BAD_ID_HINT,
        )
        raise typer.Exit(code=2)

    # 3. Settings ladder (exit 6).
    try:
        settings = get_registry_settings()
    except ValidationError as exc:
        if is_missing_encryption_key(exc):
            render_error(
                "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
                _missing_key_hint,
            )
        else:
            render_error("Configuration error", first_error_msg(exc))
        raise typer.Exit(code=6) from None
    except (OSError, PermissionError) as exc:
        render_error("Configuration error: settings file unreadable", str(exc))
        raise typer.Exit(code=6) from None

    if settings.key_registry_encryption_key is None:
        render_error(
            "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
            _missing_key_hint,
        )
        raise typer.Exit(code=6)

    # 4. Open DB + migrations (exit 5).
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from None

    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        # 5. Fetch the export row (+ associated keys).
        try:
            row = get_customer_with_cipher(conn, customer_id)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        if row is None:
            render_error(
                f"Not found: no customer matches '{customer_id}'",
                _EXPORT_NOT_FOUND_HINT,
            )
            raise typer.Exit(code=3)

        try:
            keys = list_api_keys(conn, customer_id=customer_id)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        audit_events: list[AuditEventRow] = []
        audit_events_total = 0
        if include_audit:
            # Audit rows live on customer-id targets (create_customer,
            # update_customer, delete_customer, correct_tax_id) AND on
            # key-id targets (issue_key, mark_paid, suspend_key, rotate_key,
            # revoke_key, update_key). Pull both buckets in one query so
            # the export reads as a unified history.
            try:
                target_ids = [customer_id, *(k.id for k in keys)]
                # Pre-count the total so the envelope can honestly signal
                # truncation. The COUNT runs over the same target set with
                # idx_audit_target — O(N) at most rather than O(log N), but
                # bounded by typical audit scale and only fires when the
                # operator opts in to ``--include-audit``.
                placeholders = ",".join("?" * len(target_ids))
                total_row = conn.execute(
                    f"SELECT COUNT(*) FROM audit_log "  # nosec B608  # placeholders is ",".join("?" * n) — only ? tokens
                    f"WHERE target_id IN ({placeholders})",
                    target_ids,
                ).fetchone()
                audit_events_total = int(total_row[0]) if total_row else 0
                audit_events = list_audit_events(
                    conn, target_ids, limit=_EXPORT_AUDIT_LIMIT
                )
            except (sqlite3.Error, OSError, RuntimeError) as exc:
                render_error("Database error", str(exc))
                raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # 6. Decrypt + format (exit 5 on InvalidToken / non-UTF8 plaintext /
    # wrong-length canonical — env-var swap or ciphertext tampering are the
    # only operational causes). The decrypted string lives only in the local
    # scope until it is embedded in the payload dict below.
    try:
        canonical = decrypt_tax_id(
            row.tax_id_encrypted, settings.key_registry_encryption_key
        )
        tax_id_display = format_tax_id_display(canonical, row.tax_id_kind)
    except (InvalidToken, UnicodeDecodeError, ValueError):
        render_error(
            "Database error: tax-ID ciphertext failed to decrypt",
            _EXPORT_DECRYPT_HINT,
        )
        raise typer.Exit(code=5) from None

    # 7. Build the export payload. Insertion order is the AC1 field-order
    # contract: id → name → email → tax_id_kind → tax_id → registered_at
    # → notes → keys.
    payload: dict[str, object] = {
        "id": row.id,
        "name": row.name,
        "email": row.email,
        "tax_id_kind": row.tax_id_kind,
        "tax_id": tax_id_display,
        "registered_at": row.registered_at,
        "notes": row.notes,
        "keys": [
            {
                "key_prefix": k.key_prefix,
                "status": k.status,
                "billing_mode": k.billing_mode,
                "created_at": k.created_at,
            }
            for k in keys
        ],
    }
    if include_audit:
        # ``list_audit_events`` returns DESC (newest first) for the
        # ``show --audit-tail`` UX. The export envelope is documented as
        # chronological in monetization.md §1.4, so reverse to ASC here.
        chronological = sorted(audit_events, key=lambda e: (e.occurred_at, e.id))
        payload["audit_events"] = [
            _audit_event_to_export_dict(event) for event in chronological
        ]
        # Honest truncation signalling — an LGPD Art. 18 II/V response must
        # be provably complete or provably partial. ``audit_events_total``
        # is the unfiltered count over the customer's target ids; the list
        # itself is capped at ``_EXPORT_AUDIT_LIMIT``.
        payload["audit_events_total"] = audit_events_total
        payload["audit_events_truncated"] = audit_events_total > len(audit_events)

    # 8. PII-austere INFO log. Never tax_id / name / email / ciphertext.
    _logger.info(
        "Customer exported | id=%s | key_count=%d | format=%s | include_audit=%s",
        row.id,
        len(keys),
        format,
        include_audit,
    )

    # 9. Render branch.
    if format == "json":
        render_export_json(payload)
    else:
        render_export_yaml(payload)


# ---------------------------------------------------------------------------
# `correct-tax-id` — LGPD Art. 18 III correction without losing audit history
# (Story 5.1 Gap 7). Replaces the encrypted tax-ID atomically; ``customer_id``
# and every ``api_key.customer_id`` FK survive (vs ``delete-customer --hard``
# + ``create-customer``, which orphans the audit chain).
# ---------------------------------------------------------------------------

_CORRECT_TAX_ID_NOT_FOUND_HINT = (
    "Run 'compadre-keys list-customers' to see registered records."
)
_CORRECT_TAX_ID_REASON_NUL_HINT = "Reason text must not contain NUL bytes."
_CORRECT_TAX_ID_REASON_NEWLINE_HINT = "Reason text must be a single line."
_CORRECT_TAX_ID_REASON_EMPTY_HINT = (
    "Provide --reason text with content. Whitespace-only reasons are rejected."
)
_CORRECT_TAX_ID_MUTEX_HINT = "Provide exactly one of --new-cpf or --new-cnpj."
_CORRECT_TAX_ID_REASON_MAX = 1024


@app.command("correct-tax-id")
def correct_tax_id_cmd(
    customer_id: str = typer.Argument(
        ...,
        metavar="CUSTOMER_ID",
        help=(
            "Customer ID whose tax-ID should be replaced. "
            "Example: cust_01j8xr3e4c5m7kn9p2qt8vwy."
        ),
    ),
    new_cpf: str | None = typer.Option(
        None,
        "--new-cpf",
        help=(
            "New CPF (replaces the stored value). Mutually exclusive with "
            '--new-cnpj. Example: --new-cpf "111.444.777-35".'
        ),
    ),
    new_cnpj: str | None = typer.Option(
        None,
        "--new-cnpj",
        help=(
            "New CNPJ (replaces the stored value). Mutually exclusive with "
            '--new-cpf. Example: --new-cnpj "12.345.678/0001-95".'
        ),
    ),
    reason: str = typer.Option(
        ...,
        "--reason",
        help=(
            "Free-form reason (<=1024 chars, single line). Stored in the "
            'audit log. Example: --reason "operator-typo correction".'
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the interactive confirmation prompt (for scripting).",
    ),
) -> None:
    """Replace a customer's tax-ID atomically; preserves customer_id + FKs.

    Example: compadre-keys correct-tax-id cust_01j8xr3e4c5m7kn9p2qt8vwy
             --new-cpf "111.444.777-35" --reason "typo correction"
    """
    # 1. Tax-ID mutex.
    if (new_cpf is None) == (new_cnpj is None):
        if new_cpf is not None:
            render_error(
                "Usage error: --new-cpf and --new-cnpj are mutually exclusive",
                _CORRECT_TAX_ID_MUTEX_HINT,
            )
        else:
            render_error(
                "Usage error: exactly one of --new-cpf or --new-cnpj is required",
                _CORRECT_TAX_ID_MUTEX_HINT,
            )
        raise typer.Exit(code=2)

    # 2. --reason shape (mirrors revoke-key --reason validators).
    if "\x00" in reason:
        render_error(
            "Usage error: --reason contains illegal characters",
            _CORRECT_TAX_ID_REASON_NUL_HINT,
        )
        raise typer.Exit(code=2)
    if "\n" in reason or "\r" in reason:
        render_error(
            "Usage error: --reason must be a single line",
            _CORRECT_TAX_ID_REASON_NEWLINE_HINT,
        )
        raise typer.Exit(code=2)
    stripped_reason = reason.strip()
    if not stripped_reason:
        render_error(
            "Usage error: --reason must be non-empty",
            _CORRECT_TAX_ID_REASON_EMPTY_HINT,
        )
        raise typer.Exit(code=2)
    if len(stripped_reason) > _CORRECT_TAX_ID_REASON_MAX:
        render_error(
            f"Usage error: --reason exceeds {_CORRECT_TAX_ID_REASON_MAX} characters",
            f"Trim --reason to {_CORRECT_TAX_ID_REASON_MAX} chars or fewer.",
        )
        raise typer.Exit(code=2)
    reason = stripped_reason

    # 2b. Customer ID shape validation — pre-DB so typos / pasted secrets
    # never open a connection (parity with export-customer / delete-customer).
    if not _CUSTOMER_ID_PATTERN.match(customer_id):
        render_error(
            "Usage error: invalid customer id",
            _EXPORT_BAD_ID_HINT,
        )
        raise typer.Exit(code=2)

    # 3. Resolve kind + canonical digits + checksum (exit 4 on bad CPF/CNPJ).
    if new_cpf is not None:
        new_kind: TaxIdKind = "cpf"
        digits = strip_tax_id(new_cpf)
    else:
        new_kind = "cnpj"
        digits = strip_tax_id(cast(str, new_cnpj))
    try:
        if new_kind == "cpf":
            validate_cpf(digits)
        else:
            validate_cnpj(digits)
    except InvalidTaxIdError as exc:
        render_error(exc.message, exc.hint)
        raise typer.Exit(code=2) from None

    # 4. Settings ladder (exit 6).
    try:
        settings = get_registry_settings()
    except ValidationError as exc:
        if is_missing_encryption_key(exc):
            render_error(
                "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
                _missing_key_hint,
            )
        else:
            render_error("Configuration error", first_error_msg(exc))
        raise typer.Exit(code=6) from None
    except (OSError, PermissionError) as exc:
        render_error("Configuration error: settings file unreadable", str(exc))
        raise typer.Exit(code=6) from None

    if settings.key_registry_encryption_key is None:
        render_error(
            "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
            _missing_key_hint,
        )
        raise typer.Exit(code=6)

    # 5. Open DB + migrations (exit 5). The DB ladder runs BEFORE encryption
    # so a missing customer / tombstoned target / config error short-circuits
    # without ever encrypting the operator-supplied tax-ID into a Fernet
    # ciphertext that would otherwise sit in process memory for an aborted
    # operation. LGPD memory-residence-window minimisation (review-pass).
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from None

    result: CorrectTaxIdResult | None = None
    try:
        try:
            _apply_migrations(conn)
            backfill_tax_id_lookup_if_needed(
                conn, encryption_key=settings.key_registry_encryption_key
            )
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        # 6. Pre-fetch the customer for the tombstone check + the
        # confirmation prompt's identifier; exit 3 on missing.
        existing = get_customer(conn, customer_id)
        if existing is None:
            render_error(
                f"Customer not found: {customer_id}",
                _CORRECT_TAX_ID_NOT_FOUND_HINT,
            )
            raise typer.Exit(code=3)

        # 7. Confirmation gate (Destroy-tier symmetry — irreversible
        # overwrite of operator-supplied PII). The prompt names the
        # ``customer_id`` only — the customer's ``name`` is PII that the
        # ``_logger.info`` discipline omits, so the interactive prompt
        # follows the same rule (the operator can ``show <id>`` if they
        # need to confirm identity before answering ``y``).
        if not yes:
            try:
                typer.confirm(
                    f"Replace tax-ID for {existing.id}. "
                    "The previous value will be overwritten and cannot be "
                    "recovered. Continue?",
                    default=False,
                    abort=True,
                    err=True,
                )
            except click.exceptions.Abort:
                stderr_console().print("Aborted.")
                if not sys.stdin.isatty():
                    stderr_console().print(
                        "Hint: stdin is not a TTY — pass --yes to confirm "
                        "scripted correction under nohup / systemd / docker exec."
                    )
                raise typer.Exit(code=2) from None

        # 8. Encrypt new tax-ID via the single-Fernet boundary + compute
        # the v3 lookup digest (B2 — both columns rotate atomically in the
        # ``correct_customer_tax_id`` helper).
        new_encrypted = encrypt_tax_id(digits, settings.key_registry_encryption_key)
        new_lookup = compute_tax_id_lookup(digits, settings.key_registry_encryption_key)

        # 9. Helper dispatch.
        try:
            result = correct_customer_tax_id(
                conn,
                customer_id,
                new_tax_id_encrypted=new_encrypted,
                new_tax_id_kind=new_kind,
                new_tax_id_lookup=new_lookup,
                reason=reason,
                actor=get_cli_actor(),
            )
        except sqlite3.IntegrityError as exc:
            # B2 — partial UNIQUE INDEX rejected the UPDATE because the
            # new tax-ID lookup is already held by another active row.
            # Translate to ``DuplicateTaxIdError`` (exit 2). Re-raise
            # other IntegrityErrors as a generic DB error (exit 5).
            if "tax_id_lookup" not in str(exc):
                render_error("Database error", str(exc))
                raise typer.Exit(code=5) from None
            from compadre.exceptions import DuplicateTaxIdError

            err = DuplicateTaxIdError(new_kind)
            render_error(err.message, err.hint)
            raise typer.Exit(code=2) from None
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    if result is None:
        # Customer was tombstoned between the pre-fetch and the helper
        # transaction — vanishingly rare under the single-writer CLI but
        # still surface it as a not-found rather than a stale success.
        render_error(
            f"Customer not found: {customer_id}",
            _CORRECT_TAX_ID_NOT_FOUND_HINT,
        )
        raise typer.Exit(code=3)

    # 10. PII-austere INFO log — never the new tax-ID value.
    _logger.info(
        "Tax-ID corrected | id=%s | old_kind=%s | new_kind=%s",
        result.customer.id,
        result.old_tax_id_kind,
        result.new_tax_id_kind,
    )

    suffix = (
        f"kind {result.old_tax_id_kind}→{result.new_tax_id_kind}"
        if result.old_tax_id_kind != result.new_tax_id_kind
        else f"kind {result.new_tax_id_kind}"
    )
    render_confirmation(
        f"Tax-ID corrected: {result.customer.id} ({result.customer.name}) — {suffix}"
    )


# ---------------------------------------------------------------------------
# `delete-customer --hard` — LGPD Art. 18 VI irreversible purge (Story 4.1).
# ---------------------------------------------------------------------------

_DELETE_CUSTOMER_HARD_REQUIRED_HINT = (
    "Re-run with --hard to acknowledge the irreversible purge. "
    "See --help for the full effect."
)
_DELETE_CUSTOMER_NOT_FOUND_HINT = (
    "Run 'compadre-keys list-customers' to see registered records."
)


@app.command("delete-customer")
def delete_customer_cmd(
    customer_id: str = typer.Argument(
        ...,
        metavar="CUSTOMER_ID",
        help=(
            "Customer ID to permanently purge. Example: cust_01j8xr3e4c5m7kn9p2qt8vwy."
        ),
    ),
    hard: bool = typer.Option(
        False,
        "--hard",
        help=(
            "Required acknowledgement flag. The customer record's personal "
            "data (name, email, encrypted tax ID, notes) is irreversibly "
            "purged; every associated API key is deleted; the tombstone "
            "row + audit-log entries are preserved. LGPD Art. 18 VI."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the interactive confirmation prompt (for scripting).",
    ),
) -> None:
    """Permanently purge a customer's personal data and all their API keys (LGPD).

    Example: compadre-keys delete-customer cust_01j8xr3e4c5m7kn9p2qt8vwy --hard
             compadre-keys delete-customer cust_01j8xr3e4c5m7kn9p2qt8vwy --hard --yes
    """
    # 1. --hard required-flag check (exit 2 BEFORE any DB / settings work).
    if not hard:
        render_error(
            "Usage error: --hard is required for delete-customer",
            _DELETE_CUSTOMER_HARD_REQUIRED_HINT,
        )
        raise typer.Exit(code=2)

    # 2. Customer ID shape validation (exit 2).
    if not _CUSTOMER_ID_PATTERN.match(customer_id):
        render_error(
            "Usage error: invalid customer id",
            _EXPORT_BAD_ID_HINT,
        )
        raise typer.Exit(code=2)

    # 3. Settings ladder (exit 6).
    try:
        settings = get_registry_settings()
    except ValidationError as exc:
        if is_missing_encryption_key(exc):
            render_error(
                "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
                _missing_key_hint,
            )
        else:
            render_error("Configuration error", first_error_msg(exc))
        raise typer.Exit(code=6) from None
    except (OSError, PermissionError) as exc:
        render_error("Configuration error: settings file unreadable", str(exc))
        raise typer.Exit(code=6) from None

    if settings.key_registry_encryption_key is None:
        render_error(
            "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
            _missing_key_hint,
        )
        raise typer.Exit(code=6)

    # 4. Open DB + migrations (exit 5).
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from None

    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        # 5. Pre-flight lookup — exit 3 BEFORE prompting. Confirming
        # deletion of a nonexistent row is worse UX than a clean
        # not-found.
        try:
            existing = get_customer(conn, customer_id)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        if existing is None:
            render_error(
                f"Customer not found: {customer_id}",
                _DELETE_CUSTOMER_NOT_FOUND_HINT,
            )
            raise typer.Exit(code=3)

        customer_name = existing.name
        try:
            pre_keys = len(list_api_keys(conn, customer_id=customer_id))
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        # 6. Confirmation gate (UX-DR11).
        if not yes:
            try:
                typer.confirm(
                    (
                        f"This will permanently delete customer "
                        f"{customer_name} and all associated keys "
                        f"({pre_keys} key(s)). This action cannot be "
                        f"undone. Encrypted tax ID will be purged. Continue?"
                    ),
                    default=False,
                    abort=True,
                    err=True,
                )
            except click.exceptions.Abort:
                stderr_console().print("Aborted.")
                raise typer.Exit(code=2) from None

        # 7. Helper call.
        try:
            result = delete_customer_hard(conn, customer_id, actor=get_cli_actor())
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    if result is None:
        # Concurrent tombstone between pre-flight and helper — unreachable
        # in single-writer CLI, but guarded for completeness.
        render_error(
            f"Customer not found: {customer_id}",
            _DELETE_CUSTOMER_NOT_FOUND_HINT,
        )
        raise typer.Exit(code=3)

    # 8. PII-austere INFO log.
    _logger.info(
        "Customer purged | id=%s | keys_purged=%d",
        result.customer_id,
        result.keys_purged,
    )

    # 9. ConfirmationLine (UX-DR7).
    render_confirmation(
        f"Deleted: {result.customer_id} ({result.customer_name}) "
        f"\u2014 {result.keys_purged} keys purged"
    )
