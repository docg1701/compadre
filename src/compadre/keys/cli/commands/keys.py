"""`compadre-keys issue-key` (and future API-key subcommands).

Exit-code contract (UX-DR10):
  0 — success
  2 — usage error (missing/illegal flags, bad billing-mode combo)
  3 — not-found (customer missing or tombstoned)
  5 — database error (sqlite3.Error, hash collision)
  6 — permission / configuration error (missing encryption key,
      invalid IANA timezone)
"""

from __future__ import annotations

import contextlib
import math
import sqlite3
import sys
from datetime import UTC, date, datetime
from typing import cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
import typer
from pydantic import ValidationError

from compadre.config import RegistrySettings, get_registry_settings
from compadre.keys.audit import get_cli_actor
from compadre.keys.billing import ManualBillingProvider, days_until, first_charge_date
from compadre.keys.cli import _clock
from compadre.keys.cli._errors import first_error_msg, is_missing_encryption_key
from compadre.keys.cli.output import (
    LIST_FORMAT_CHOICES,
    _format_brl,
    render_confirmation,
    render_empty_state,
    render_error,
    render_json,
    render_key_list,
    render_key_panel,
    stderr_console,
)
from compadre.keys.db import (
    RotateKeyResult,
    UpdateKeyResult,
    _apply_migrations,
    generate_key_id,
    get_connection,
    get_customer,
    insert_api_key,
    is_first_run_for_keys,
    list_api_keys,
    resolve_show_target,
    revoke_api_key,
    rotate_api_key,
    update_api_key,
)
from compadre.keys.models import (
    ApiKeyCreate,
    ApiKeyListRow,
    ApiKeyRecord,
    BillingMode,
    CustomerStatus,
    IssuedKey,
    KeyDetailRow,
)
from compadre.keys.tokens import (
    generate_api_key_plaintext,
    hash_api_key,
    key_prefix_from_plaintext,
)
from compadre.logging import get_logger

_logger = get_logger("keys.cli")

app = typer.Typer(help="API key registry commands.", no_args_is_help=True)

_VALID_BILLING_MODES = ("manual", "complimentary")

_MISSING_KEY_HINT = (
    "Generate one with: python -c 'from cryptography.fernet import "
    "Fernet; print(Fernet.generate_key().decode())' and add it to "
    ".env. Restart the service if monetization is enabled there too."
)

# ---------------------------------------------------------------------------
# Story 1.6 — shared hint constants for update-key / rotate-key / revoke-key.
# ---------------------------------------------------------------------------

_KEY_RPM_MAX = 10_000
_KEY_NOTES_MAX = 1024
_KEY_IDENT_ECHO_MAX = 80  # AC7 — truncate operator input in error titles

_UPDATE_KEY_NEITHER_HINT = (
    'Provide --rpm N, --price R$, or --notes "text" (any combination).'
)
_UPDATE_KEY_NOTES_NUL_HINT = "Notes text must not contain NUL bytes."
_UPDATE_KEY_NOTES_NEWLINE_HINT = (
    "Notes text must be a single line. Use update-customer --add-note "
    "for multi-line narrative."
)
_UPDATE_KEY_NOTES_OVERSIZE_HINT = (
    "Operator notes are capped at 1024 characters per update. Summarize "
    "or split across multiple update-key calls."
)
_UPDATE_KEY_RPM_RANGE_HINT = "Provide --rpm in the range 1..10000."
_UPDATE_KEY_COMPLIMENTARY_HINT = (
    "Complimentary keys are bundled externally; omit --price or rotate "
    "via revoke-key + issue-key."
)
_UPDATE_KEY_REVOKED_HINT = (
    "Revoked keys are immutable. Issue a new key via issue-key if the customer returns."
)

_KEY_NOT_FOUND_HINT = "Run 'compadre-keys list-keys' to see issued keys."
_KEY_AMBIGUOUS_HINT = (
    "Use the full key_<ulid> identifier or rotate one of the colliding keys."
)
_KEY_CUSTOMER_ID_HINT = (
    "Pass an 8-char key prefix, a key_<ulid>, or the pasted plaintext "
    "(cmpd_live_...). Not a customer ID."
)

_ROTATE_KEY_REVOKED_HINT = (
    "Revoked keys cannot be rotated. Issue a new key via issue-key if the "
    "customer returns."
)
_ROTATE_KEY_HASH_COLLISION_HINT = (
    "Retry the command — this is vanishingly rare. If it repeats, check disk integrity."
)

_REVOKE_KEY_ALREADY_HINT = (
    "Revocation is terminal. Issue a new key via issue-key if the customer returns."
)
_REVOKE_KEY_REASON_EMPTY_HINT = (
    "Provide reason text with content. Whitespace-only reasons are rejected."
)
_REVOKE_KEY_REASON_NUL_HINT = "Reason text must not contain NUL bytes."
_REVOKE_KEY_REASON_NEWLINE_HINT = (
    "Reason text must be a single line. Newlines would forge dated entries "
    "in the stored audit row."
)
_REVOKE_KEY_REASON_OVERSIZE_HINT = (
    "Revocation reasons are capped at 1024 characters. Summarize or store "
    "the detail in customer notes."
)


def _truncate_ident(raw: str) -> str:
    """Return ``raw`` or its first-80-char-plus-ellipsis truncation.

    Closes the Story 1.2 deferred-work item "Raw ``--customer`` string
    interpolated into error title with no length cap" for every command
    that echoes the operator's positional argument. A multi-KB paste
    would otherwise blow up the red ErrorDisplay panel title.
    """
    if len(raw) <= _KEY_IDENT_ECHO_MAX:
        return raw
    return raw[:_KEY_IDENT_ECHO_MAX] + "\u2026"


@app.command("issue-key")
def issue_key(
    customer: str = typer.Option(
        ...,
        "--customer",
        help=(
            "Customer ID from create-customer. "
            'Example: "cust_01j8xr3e4c5m7kn9p2qt8vwy".'
        ),
    ),
    rpm: int = typer.Option(
        ...,
        "--rpm",
        help="Per-key requests-per-minute limit (1..10000). Example: --rpm 60.",
    ),
    billing_mode: str = typer.Option(
        "manual",
        "--billing-mode",
        help="manual (default) or complimentary. Example: --billing-mode manual.",
    ),
    price: float | None = typer.Option(
        None,
        "--price",
        "--price-monthly",
        help=(
            "Monthly price in BRL. Required for manual billing. "
            "Example: --price 149. ``--price-monthly`` is accepted as an "
            "alias for symmetry with ``mark-paid --price-monthly``."
        ),
    ),
    notes: str = typer.Option(
        "",
        "--notes",
        help=(
            "Free-form operator notes. "
            'Example: "bundled into infra package R$ 850/month".'
        ),
    ),
) -> None:
    """Issue an API key for a registered customer (one-shot plaintext display)."""
    # 1. billing-mode literal.
    if billing_mode not in _VALID_BILLING_MODES:
        render_error(
            "Usage error: --billing-mode must be manual or complimentary",
            "Valid values: manual, complimentary.",
        )
        raise typer.Exit(code=2)

    # 2. billing-mode / price combination.
    if billing_mode == "manual" and price is None:
        render_error(
            "Usage error: --price is required for manual billing mode",
            "Specify --price <R$ per month> or use --billing-mode complimentary.",
        )
        raise typer.Exit(code=2)
    if billing_mode == "complimentary" and price is not None:
        render_error(
            "Usage error: --price is not applicable to complimentary keys",
            (
                "Complimentary keys are bundled externally; omit --price or "
                "change --billing-mode to manual."
            ),
        )
        raise typer.Exit(code=2)
    if billing_mode == "manual":
        assert price is not None  # guarded above
        if not math.isfinite(price):
            render_error(
                "Usage error: --price must be a finite number",
                "Provide a positive --price in BRL, e.g. --price 149.",
            )
            raise typer.Exit(code=2)
        if price <= 0:
            # Zero / negative prices must route through complimentary mode.
            # That mode exists precisely for bundled customers where Compadre
            # is included in a larger package and no per-key charge applies.
            render_error(
                "Usage error: --price must be positive for manual billing mode",
                (
                    "Use --billing-mode complimentary for bundled / no-charge "
                    "customers, or provide a positive --price in BRL."
                ),
            )
            raise typer.Exit(code=2)
    effective_price = (
        0.0 if billing_mode == "complimentary" else float(cast(float, price))
    )

    # 3. rpm bounds (1..10_000 inclusive) — mirrors update-key's _KEY_RPM_MAX
    # cap so the --rpm surface is consistent across commands. Closes the
    # Story 1.2 / 1.6 deferred-work item "issue-key --rpm upper bound".
    if not 1 <= rpm <= _KEY_RPM_MAX:
        render_error(
            f"Usage error: --rpm must be between 1 and {_KEY_RPM_MAX}",
            _UPDATE_KEY_RPM_RANGE_HINT,
        )
        raise typer.Exit(code=2)

    # 4. Settings + encryption-key sanity (F38 — adopt shared ladder).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded

    # 5. Open DB, apply migrations, look up customer.
    #    Customer lookup runs BEFORE ApiKeyCreate validation so that
    #    syntactically invalid ids (e.g., ``cust_nope``) yield exit 3
    #    "Customer not found" rather than exit 2 "usage error" — the
    #    operator sees the actionable message (AC3).
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

        customer_record = get_customer(conn, customer)
        if customer_record is None:
            render_error(
                f"Customer not found: {customer}",
                "Run 'compadre-keys list-customers' to see registered customers.",
            )
            raise typer.Exit(code=3)

        # 6. ApiKeyCreate input validation — defensive. Customer id is a DB row
        # id at this point, so format-validation is redundant but cheap.
        try:
            ApiKeyCreate(
                customer_id=customer,
                rate_limit_rpm=rpm,
                billing_mode=billing_mode,
                price_monthly=effective_price,
                notes=notes,
            )
        except ValidationError as exc:
            render_error(
                "Usage error: invalid issue-key parameters",
                first_error_msg(exc),
            )
            raise typer.Exit(code=2) from None

        # 7. Generate plaintext + hash + prefix + id. Plaintext stays in this
        # local scope; the record and IssuedKey wrappers carry only hash/prefix
        # and the plaintext reference until render_key_panel returns.
        plaintext = generate_api_key_plaintext()
        key_hash_hex = hash_api_key(plaintext)
        key_prefix = key_prefix_from_plaintext(plaintext)
        key_id = generate_key_id()
        created_at = datetime.now(UTC).replace(microsecond=0)

        # 8. First-charge date (manual only).
        if billing_mode == "manual":
            today_local = _clock.resolve_today_in_tz(billing_tz)
            fc_date = first_charge_date(
                today=today_local,
                billing_day=settings.billing_day,
                tz=settings.billing_timezone,
            )
            fc_iso: str | None = fc_date.isoformat()
            fc_days_free: int | None = days_until(fc_date, from_date=today_local)
        else:
            fc_iso = None
            fc_days_free = None

        record = ApiKeyRecord(
            id=key_id,
            customer_id=customer,
            key_hash=key_hash_hex,
            key_prefix=key_prefix,
            rate_limit_rpm=rpm,
            status="active",
            billing_mode=cast(BillingMode, billing_mode),
            price_monthly=effective_price,
            paid_until=None,
            created_at=created_at,
            last_used_at=None,
            notes=notes,
        )

        # 9. Insert + audit in one transaction.
        try:
            insert_api_key(conn, record, actor=get_cli_actor())
        except sqlite3.IntegrityError as exc:
            # Prefer sqlite_errorcode (stable since Python 3.11) over locale-
            # dependent message-text matching. Fall back to substring for
            # robustness against older SQLite builds.
            errorcode = getattr(exc, "sqlite_errorcode", None)
            args_msg = str(exc.args[0]) if exc.args else ""
            is_unique = errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE or (
                "UNIQUE" in args_msg.upper()
            )
            if is_unique and "key_hash" in args_msg:
                render_error(
                    "Database error: key hash collision",
                    (
                        "Retry the command — this is vanishingly rare. "
                        "If it repeats, check disk integrity."
                    ),
                )
            else:
                render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
        except ValueError as exc:
            # log_event's audit-blocklist guard raises ValueError when an
            # attempt to store personal data in audit details is detected.
            # The `with conn:` block in insert_api_key has already rolled
            # back the api_key row, so we only need to surface the error.
            render_error(
                "Audit error: refused to write audit record",
                str(exc),
            )
            raise typer.Exit(code=5) from None
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        # 10. Billing hook (MVP no-op).
        ManualBillingProvider().on_key_issued(record)
    finally:
        # Suppress close errors post-commit — the row is already durable and
        # a surfaced close error would make a successful issuance look failed.
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # 11. Structured INFO log — NEVER plaintext, NEVER hash, NEVER customer_name.
    _logger.info(
        "Key issued | id=%s | customer_id=%s | billing_mode=%s | rate_limit_rpm=%s",
        record.id,
        record.customer_id,
        record.billing_mode,
        record.rate_limit_rpm,
    )

    # 12. One-shot display. After render_key_panel returns, the local
    # plaintext variable goes out of scope at function end.
    issued = IssuedKey(
        plaintext=plaintext,
        record=record,
        customer_name=customer_record.name,
        first_charge_date=fc_iso,
        first_charge_days_free=fc_days_free,
    )
    render_key_panel(issued)


_VALID_STATUSES: tuple[str, ...] = ("active", "suspended", "revoked")

_FIRST_RUN_KEY_HINT = (
    "To issue one:"
    " compadre-keys issue-key --customer <customer-id> --rpm 60 --price 149"
)


def _key_row_to_json(row: ApiKeyListRow) -> dict[str, object]:
    """Serialize an ``ApiKeyListRow`` to the AC8 JSON shape (fixed key order)."""
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
        "notes": row.notes,
    }


def _resolve_title(
    *,
    customer_name: str | None,
    billing_mode: str | None,
    status: str | None,
    paid_until_lte: str | None,
) -> str:
    """Return the single most-specific table title for the active filter set.

    Precedence (AC6): --customer > --billing-mode > --status > --paid-until-lte
    > default "Keys". Stacking adjectives would blow past the 80-col budget
    and the table data already reflects the composed WHERE clause — the
    title just advertises the operator's intent at a glance.
    """
    if customer_name is not None:
        return f"Keys for {customer_name}"
    if billing_mode is not None:
        return "Manual Keys" if billing_mode == "manual" else "Complimentary Keys"
    if status is not None:
        return f"{status.capitalize()} Keys"
    if paid_until_lte is not None:
        return f"Keys (paid until \u2264 {paid_until_lte})"
    return "Keys"


@app.command("list-keys")
def list_keys_cmd(
    status: str | None = typer.Option(
        None,
        "--status",
        help=(
            "Filter by status: active, suspended, or revoked. "
            "Example: --status suspended."
        ),
    ),
    billing_mode: str | None = typer.Option(
        None,
        "--billing-mode",
        help=(
            "Filter by billing mode: manual or complimentary. "
            "Example: --billing-mode complimentary."
        ),
    ),
    customer: str | None = typer.Option(
        None,
        "--customer",
        help=(
            "Filter by customer ID. "
            'Example: --customer "cust_01j8xr3e4c5m7kn9p2qt8vwy".'
        ),
    ),
    paid_until_lte: str | None = typer.Option(
        None,
        "--paid-until-lte",
        help=(
            "Filter by paid-until date (YYYY-MM-DD) or 'today'. "
            "Example: --paid-until-lte today."
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
    """List API keys with filters and color-coded status.

    Example: compadre-keys list-keys --status active --billing-mode manual
    --paid-until-lte today
    """
    # 1. Literal filter validation (UX-DR10 exit 2).
    if status is not None and status not in _VALID_STATUSES:
        render_error(
            "Usage error: --status must be active, suspended, or revoked",
            "Valid values: active, suspended, revoked.",
        )
        raise typer.Exit(code=2)
    if billing_mode is not None and billing_mode not in ("manual", "complimentary"):
        render_error(
            "Usage error: --billing-mode must be manual or complimentary",
            "Valid values: manual, complimentary.",
        )
        raise typer.Exit(code=2)
    if format not in LIST_FORMAT_CHOICES:
        render_error(
            "Usage error: --format must be table or json",
            "Valid values: table, json.",
        )
        raise typer.Exit(code=2)

    # Empty-string --customer bypasses the None guard (Typer preserves the
    # literal ""). Reject upfront so the title and SQL lookup stay sane.
    if customer is not None and not customer.strip():
        render_error(
            "Usage error: --customer must not be empty",
            'Provide a customer ID, e.g. --customer "cust_01j8xr3e4c5m7kn9p2qt8vwy".',
        )
        raise typer.Exit(code=2)

    # 2. Settings + billing tz (F38 — adopt shared ladder).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded

    # 3. --paid-until-lte resolution ("today" or ISO date). Stripping whitespace
    # first so copy-pasted values don't reject; re-emitting the parsed date
    # via isoformat() normalizes alternate ISO forms (e.g. "20260415") that
    # date.fromisoformat accepts but would compare lexicographically wrong
    # against the YYYY-MM-DD rows in storage.
    resolved_paid: str | None
    if paid_until_lte is None:
        resolved_paid = None
    else:
        trimmed = paid_until_lte.strip()
        if trimmed == "today":
            resolved_paid = _clock.resolve_today_in_tz(billing_tz).isoformat()
        else:
            try:
                parsed = date.fromisoformat(trimmed)
            except ValueError:
                render_error(
                    "Usage error: --paid-until-lte must be an ISO date "
                    "(YYYY-MM-DD) or 'today'",
                    "Example: --paid-until-lte 2026-04-15 or --paid-until-lte today.",
                )
                raise typer.Exit(code=2) from None
            resolved_paid = parsed.isoformat()

    # 4. Open DB + apply migrations + read rows.
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

        rows = list_api_keys(
            conn,
            status=cast(CustomerStatus | None, status),
            billing_mode=cast(BillingMode | None, billing_mode),
            customer_id=customer,
            paid_until_lte=resolved_paid,
        )

        # Customer title resolution — look up the name when --customer is set
        # so the title reads "Keys for Helena Ribeiro" instead of the raw id.
        customer_name_for_title: str | None = None
        if customer is not None:
            if rows:
                customer_name_for_title = rows[0].customer_name
            else:
                record_lookup = get_customer(conn, customer)
                customer_name_for_title = (
                    record_lookup.name if record_lookup is not None else customer
                )

        first_run = is_first_run_for_keys(conn) if not rows else False
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    _logger.info(
        "Listed keys | count=%d | status=%s | billing_mode=%s | "
        "customer=%s | paid_until_lte=%s",
        len(rows),
        status,
        billing_mode,
        customer,
        resolved_paid,
    )

    # 5. Branch on --format. JSON path NEVER emits hint text (AC8).
    if format == "json":
        render_json([_key_row_to_json(r) for r in rows])
        return

    title = _resolve_title(
        customer_name=customer_name_for_title,
        billing_mode=billing_mode,
        status=status,
        paid_until_lte=resolved_paid,
    )

    if rows:
        render_key_list(title, rows)
        return

    # Empty branches — AC7.
    if customer is not None:
        render_empty_state([f"No keys found for customer {customer}."])
        return

    if first_run:
        render_empty_state(["No keys issued yet.", _FIRST_RUN_KEY_HINT])
        return

    if status is not None:
        render_empty_state([f"No {status} keys found."])
        return

    if billing_mode is not None:
        label = "manual" if billing_mode == "manual" else "complimentary"
        render_empty_state([f"No {label} keys found."])
        return

    render_empty_state(["No keys found."])


# ---------------------------------------------------------------------------
# Story 1.6 — update-key / rotate-key / revoke-key.
# ---------------------------------------------------------------------------


def _key_update_clauses(result: UpdateKeyResult) -> list[str]:
    """Assemble the ConfirmationLine clause list for ``update-key``.

    Fixed order ``rpm -> price -> notes`` matches the table-like mental
    order operators use when reading ``show <prefix>`` output. The arrow
    glyph is U+2192 (RIGHTWARDS ARROW); the em-dash between key-prefix and
    suffix is U+2014 (set by the caller). ``changes`` carries pre-/post-
    mutation numeric values so no second DB read is needed.
    """
    clauses: list[str] = []
    if "rpm" in result.updated_fields:
        clauses.append(f"rpm: {result.changes.rpm_old}\u2192{result.changes.rpm_new}")
    if "price" in result.updated_fields:
        # Runtime checks rather than ``assert`` — the latter is stripped
        # under ``python -O`` / ``PYTHONOPTIMIZE=1`` per the ``keys/audit.py``
        # docstring convention, which would let ``None`` slip through to
        # ``_format_brl`` and raise an unhelpful ``TypeError``.
        if result.changes.price_old is None or result.changes.price_new is None:
            raise RuntimeError(
                "_key_update_clauses: 'price' in updated_fields but deltas missing"
            )
        clauses.append(
            f"price: R$ {_format_brl(result.changes.price_old)}"
            f"\u2192R$ {_format_brl(result.changes.price_new)}"
        )
    if "notes" in result.updated_fields:
        clauses.append("notes updated")
    return clauses


def load_settings_and_tz() -> tuple[RegistrySettings, ZoneInfo] | None:
    """Return ``(settings, billing_tz)`` or ``None`` after rendering an error.

    Shared configuration-surface for the Story 1.6 and Story 3.1 command
    modules so the encryption-key / billing-TZ exit-6 mapping stays DRY
    without touching the existing ``issue-key`` / ``list-keys`` / ``show``
    paths.

    F33 — switched from ``Settings`` to ``RegistrySettings`` so the CLI
    no longer drags STT-runtime fields (faster-whisper paths, model URLs)
    through pydantic validation. Hosts that have not
    yet deployed the FastAPI app can still operate the registry.

    F36 — return type tightened from ``tuple[object, ZoneInfo] | None``
    so call sites stop needing ``cast(Settings, settings_obj)``.

    Returns ``None`` when the caller should ``raise typer.Exit(code=6)``;
    the error panel has already been rendered.
    """
    try:
        settings = get_registry_settings()
    except ValidationError as exc:
        if is_missing_encryption_key(exc):
            render_error(
                "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
                _MISSING_KEY_HINT,
            )
        else:
            render_error("Configuration error", first_error_msg(exc))
        return None
    except (OSError, PermissionError) as exc:
        render_error("Configuration error: settings file unreadable", str(exc))
        return None

    if settings.key_registry_encryption_key is None:
        render_error(
            "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
            _MISSING_KEY_HINT,
        )
        return None

    try:
        billing_tz = ZoneInfo(settings.billing_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        render_error(
            "Configuration error: BILLING_TIMEZONE is not a valid IANA timezone",
            f"Set BILLING_TIMEZONE to a valid zone (e.g., America/Sao_Paulo). "
            f"Got: {settings.billing_timezone}",
        )
        return None

    return settings, billing_tz


def resolve_key_target(
    conn: sqlite3.Connection,
    raw_target: str,
    today: date,
) -> KeyDetailRow:
    """Resolve ``raw_target`` to a ``KeyDetailRow`` or raise ``typer.Exit``.

    Rejects the ``customer`` / ``not_found`` / ``key_ambiguous`` branches
    of ``resolve_show_target`` with the right error panels and exit codes
    for the Story 1.6 + Story 3.1 key-verb commands. All six commands
    share the same four-branch map, so the dispatch lives here to keep
    each command body focused on its own validation ladder.
    """
    resolved = resolve_show_target(conn, raw_target, today=today)
    echoed = _truncate_ident(raw_target)
    if resolved.kind == "not_found":
        render_error(f"Key not found: {echoed}", _KEY_NOT_FOUND_HINT)
        raise typer.Exit(code=3)
    if resolved.kind == "key_ambiguous":
        render_error(
            f"Ambiguous prefix: multiple keys match '{echoed}'",
            _KEY_AMBIGUOUS_HINT,
        )
        raise typer.Exit(code=3)
    if resolved.kind == "customer":
        render_error(
            "Usage error: key identifier required (got customer ID)",
            _KEY_CUSTOMER_ID_HINT,
        )
        raise typer.Exit(code=2)
    # kind == "key_single".
    return cast(KeyDetailRow, resolved.value)


@app.command("update-key")
def update_key_cmd(
    key_prefix: str = typer.Argument(
        ...,
        metavar="KEY_PREFIX",
        help=(
            "8-char key prefix, key_<ulid>, or pasted cmpd_live_<plaintext>. "
            'Example: "Pq_abc12".'
        ),
    ),
    rpm: int | None = typer.Option(
        None,
        "--rpm",
        help="Per-key requests-per-minute limit (1..10000). Example: --rpm 120.",
    ),
    price: float | None = typer.Option(
        None,
        "--price",
        "--price-monthly",
        help=(
            "Monthly price in BRL for manual billing. Example: --price 199. "
            "``--price-monthly`` accepted as an alias for symmetry with "
            "``mark-paid --price-monthly``."
        ),
    ),
    notes: str | None = typer.Option(
        None,
        "--notes",
        help=(
            "Replace stored operator notes (<=1024 chars, single line). "
            'Example: --notes "upgraded 2026-05-12 from 60\u2192120rpm".'
        ),
    ),
) -> None:
    """Update a key's RPM, monthly price, and/or operator notes.

    Example: compadre-keys update-key Pq_abc12 --rpm 120 --price 199
    """
    # 1. Neither-flag check — cheapest usage mistake, no DB work.
    if rpm is None and price is None and notes is None:
        render_error(
            "Usage error: at least one of --rpm, --price, or --notes is required",
            _UPDATE_KEY_NEITHER_HINT,
        )
        raise typer.Exit(code=2)

    # 2. --rpm bounds (1..10000 inclusive). Typer converts to int; we only
    # police the value range.
    if rpm is not None and not 1 <= rpm <= _KEY_RPM_MAX:
        render_error(
            f"Usage error: --rpm must be between 1 and {_KEY_RPM_MAX}",
            _UPDATE_KEY_RPM_RANGE_HINT,
        )
        raise typer.Exit(code=2)

    # 3. --price shape checks (mirrors issue-key AC2).
    if price is not None:
        if not math.isfinite(price):
            render_error(
                "Usage error: --price must be a finite number",
                "Provide a positive --price in BRL, e.g. --price 199.",
            )
            raise typer.Exit(code=2)
        if price <= 0:
            render_error(
                "Usage error: --price must be positive for manual billing mode",
                (
                    "Use --billing-mode complimentary at issue-key time for "
                    "bundled customers, or provide a positive --price in BRL."
                ),
            )
            raise typer.Exit(code=2)

    # 4. --notes shape + length checks (mirrors Story 1.5 --add-note ladder).
    # Strip outer whitespace before length / storage to match the
    # ``revoke-key --reason`` and ``update-customer --add-note`` convention.
    if notes is not None:
        if "\x00" in notes:
            render_error(
                "Usage error: --notes contains illegal characters",
                _UPDATE_KEY_NOTES_NUL_HINT,
            )
            raise typer.Exit(code=2)
        if "\n" in notes or "\r" in notes:
            render_error(
                "Usage error: --notes must be a single line",
                _UPDATE_KEY_NOTES_NEWLINE_HINT,
            )
            raise typer.Exit(code=2)
        notes = notes.strip()
        if len(notes) > _KEY_NOTES_MAX:
            render_error(
                f"Usage error: --notes exceeds {_KEY_NOTES_MAX} characters",
                _UPDATE_KEY_NOTES_OVERSIZE_HINT,
            )
            raise typer.Exit(code=2)

    # 5. Settings + TZ (exit 6).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded
    today = _clock.resolve_today_in_tz(billing_tz)

    # 6. Open DB + migrations (exit 5).
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

        # 7. Resolve prefix → KeyDetailRow (exit 3 / 2 on non-key branches).
        key_row = resolve_key_target(conn, key_prefix, today=today)

        # 8. Complimentary + --price mutex (exit 2). Placed AFTER resolve so
        # missing-key → exit 3 takes precedence over usage-error echoing a
        # non-existent key.
        if key_row.billing_mode == "complimentary" and price is not None:
            render_error(
                "Usage error: --price is not applicable to complimentary keys",
                _UPDATE_KEY_COMPLIMENTARY_HINT,
            )
            raise typer.Exit(code=2)

        # 9. Helper call — exit 3 on revoked, exit 5 on sqlite / RuntimeError.
        try:
            result = update_api_key(
                conn,
                key_row.id,
                new_rpm=rpm,
                new_price=price,
                new_notes=notes,
                actor=get_cli_actor(),
                today=today,
            )
        except ValueError as exc:
            msg = str(exc)
            if msg == "cannot update a revoked key":
                render_error(
                    f"Key is revoked: {_truncate_ident(key_prefix)}",
                    _UPDATE_KEY_REVOKED_HINT,
                )
                raise typer.Exit(code=3) from None
            if msg == "cannot set --price on a complimentary key":
                # Defense-in-depth — step 8 catches this first in practice.
                render_error(
                    "Usage error: --price is not applicable to complimentary keys",
                    _UPDATE_KEY_COMPLIMENTARY_HINT,
                )
                raise typer.Exit(code=2) from None
            # Unknown ValueError shape — re-raise so the operator sees a
            # traceback rather than a silently-misrouted error panel.
            raise
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # ``result`` is not None — ``resolve_show_target`` already confirmed the row.
    assert result is not None

    # 10. Structured INFO log (stderr). Suppress handler faults so a
    # successful mutation never looks failed. AC18 payload shape.
    with contextlib.suppress(Exception):
        _logger.info(
            "Updated key | id=%s | fields=%s",
            result.key.id,
            ",".join(result.updated_fields),
        )

    # 11. ConfirmationLine with the fixed arrow-form grammar.
    # B25 \u2014 if every supplied field already matched its stored value the
    # helper returns ``updated_fields=()``; mirror ``update_customer``'s
    # no-op rendering instead of "Updated: ... \u2014 " with an empty suffix.
    if not result.updated_fields:
        render_confirmation(
            f"No changes: {result.key.key_prefix} ({result.key.customer_name})"
            " \u2014 already up to date"
        )
        return
    clauses = _key_update_clauses(result)
    suffix = ", ".join(clauses)
    render_confirmation(
        f"Updated: {result.key.key_prefix} ({result.key.customer_name}) \u2014 {suffix}"
    )


@app.command("rotate-key")
def rotate_key_cmd(
    key_prefix: str = typer.Argument(
        ...,
        metavar="KEY_PREFIX",
        help=(
            "8-char key prefix, key_<ulid>, or pasted cmpd_live_<plaintext>. "
            'Example: "Pq_abc12".'
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the interactive confirmation prompt (for scripting).",
    ),
) -> None:
    """Rotate an API key — same key_<id>, fresh plaintext, old hash dies.

    Example: compadre-keys rotate-key Pq_abc12
    """
    # 1. Settings + TZ (exit 6).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded
    today = _clock.resolve_today_in_tz(billing_tz)

    # 2. Open DB + migrations (exit 5).
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from None

    result: RotateKeyResult | None = None
    new_plaintext: str | None = None
    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        # 3. Resolve prefix → KeyDetailRow.
        key_row = resolve_key_target(conn, key_prefix, today=today)

        # 4. Reject revoked rotation BEFORE generating entropy (story scope
        # guard §"Do NOT generate the new plaintext before the revocation
        # check"). The helper's in-transaction status check remains load-
        # bearing as TOCTOU defense-in-depth, but the CLI's pre-check spares
        # the ``secrets.token_urlsafe(32)`` call on the reject branch.
        if key_row.status == "revoked":
            render_error(
                f"Key is revoked: {_truncate_ident(key_prefix)}",
                _ROTATE_KEY_REVOKED_HINT,
            )
            raise typer.Exit(code=3)

        # 4b. Confirmation gate — symmetry with revoke-key / delete-customer
        # / rotate-encryption-key. Rotation invalidates the old plaintext
        # immediately, so scripted callers MUST pass --yes; interactive
        # operators get the prompt.
        if not yes:
            try:
                typer.confirm(
                    f"This will rotate key {key_row.key_prefix} — the old "
                    f"plaintext stops working immediately. Continue?",
                    default=False,
                    abort=True,
                    err=True,
                )
            except click.exceptions.Abort:
                stderr_console().print("Aborted.")
                if not sys.stdin.isatty():
                    stderr_console().print(
                        "Hint: stdin is not a TTY — pass --yes to confirm "
                        "scripted rotation under nohup / systemd / docker exec."
                    )
                raise typer.Exit(code=2) from None

        # 5. Generate the new plaintext.
        new_plaintext = generate_api_key_plaintext()

        # 5. Helper call with the hash-collision / revoked / runtime ladder.
        try:
            result = rotate_api_key(
                conn,
                key_row.id,
                new_plaintext=new_plaintext,
                actor=get_cli_actor(),
            )
        except ValueError as exc:
            if str(exc) == "cannot rotate a revoked key":
                render_error(
                    f"Key is revoked: {_truncate_ident(key_prefix)}",
                    _ROTATE_KEY_REVOKED_HINT,
                )
                raise typer.Exit(code=3) from None
            raise  # defensive — no other ValueError shape today.
        except sqlite3.IntegrityError as exc:
            errorcode = getattr(exc, "sqlite_errorcode", None)
            args_msg = str(exc.args[0]) if exc.args else ""
            is_unique = errorcode == sqlite3.SQLITE_CONSTRAINT_UNIQUE or (
                "UNIQUE" in args_msg.upper()
            )
            if is_unique and "key_hash" in args_msg:
                render_error(
                    "Database error: key hash collision",
                    _ROTATE_KEY_HASH_COLLISION_HINT,
                )
            else:
                render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    assert result is not None
    assert new_plaintext is not None

    # 6. Structured INFO log — prefix deltas only, never the plaintext / hash.
    with contextlib.suppress(Exception):
        _logger.info(
            "Key rotated | id=%s | key_prefix_old=%s | key_prefix_new=%s",
            result.record.id,
            result.old_prefix,
            result.record.key_prefix,
        )

    # 7. Render the one-shot KeyPanel. ``first_charge_date=None`` triggers
    # the Story 1.6 ``_context_block`` branch that shows ``paid until:``
    # instead of ``first charge:`` — rotation preserves billing state.
    issued = IssuedKey(
        plaintext=new_plaintext,
        record=result.record,
        customer_name=result.customer_name,
        first_charge_date=None,
        first_charge_days_free=None,
    )
    render_key_panel(issued)


@app.command("revoke-key")
def revoke_key_cmd(
    key_prefix: str = typer.Argument(
        ...,
        metavar="KEY_PREFIX",
        help=(
            "8-char key prefix, key_<ulid>, or pasted cmpd_live_<plaintext>. "
            'Example: "Pq_abc12".'
        ),
    ),
    reason: str = typer.Option(
        ...,
        "--reason",
        help=(
            "Free-form reason (<=1024 chars, single line). Stored in the audit "
            'log. Example: --reason "bundle contract ended 2026-09-30".'
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the interactive confirmation prompt (for scripting).",
    ),
) -> None:
    """Permanently revoke an API key (destructive; --yes or interactive confirm).

    Example: compadre-keys revoke-key Pq_abc12
             --reason "bundle contract ended 2026-09-30"
    """
    # 1. --reason shape validation. NUL / newline checks fire on the raw
    # string, length and empty checks on the stripped form — operators who
    # paste whitespace-padded text still get the canonical audit row.
    if "\x00" in reason:
        render_error(
            "Usage error: --reason contains illegal characters",
            _REVOKE_KEY_REASON_NUL_HINT,
        )
        raise typer.Exit(code=2)
    if "\n" in reason or "\r" in reason:
        render_error(
            "Usage error: --reason must be a single line",
            _REVOKE_KEY_REASON_NEWLINE_HINT,
        )
        raise typer.Exit(code=2)
    stripped_reason = reason.strip()
    if not stripped_reason:
        render_error(
            "Usage error: --reason must be non-empty",
            _REVOKE_KEY_REASON_EMPTY_HINT,
        )
        raise typer.Exit(code=2)
    if len(stripped_reason) > _KEY_NOTES_MAX:
        render_error(
            f"Usage error: --reason exceeds {_KEY_NOTES_MAX} characters",
            _REVOKE_KEY_REASON_OVERSIZE_HINT,
        )
        raise typer.Exit(code=2)
    reason = stripped_reason

    # 2. Settings + TZ (exit 6).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded
    today = _clock.resolve_today_in_tz(billing_tz)

    # 3. Open DB + migrations (exit 5).
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

        # 4. Resolve prefix → KeyDetailRow.
        key_row = resolve_key_target(conn, key_prefix, today=today)

        # 5. Confirmation gate (UX-DR11 Destroy-tier pattern).
        if not yes:
            try:
                typer.confirm(
                    f"This will permanently revoke key {key_row.key_prefix}. Continue?",
                    default=False,
                    abort=True,
                    err=True,
                )
            except click.exceptions.Abort:
                # Click writes nothing when we catch before escape — emit our
                # own terminal message and remap exit 1 (Click default) to
                # exit 2 (usage tier per UX-DR10). When stdin is not a TTY,
                # add a hint pointing operators toward --yes so scripted
                # invocations under nohup / systemd / docker exec do not
                # die with a bare "Aborted." with no actionable signal.
                stderr_console().print("Aborted.")
                if not sys.stdin.isatty():
                    stderr_console().print(
                        "Hint: stdin is not a TTY — pass --yes to confirm "
                        "scripted revocation under nohup / systemd / docker exec."
                    )
                raise typer.Exit(code=2) from None

        # 6. Helper call.
        try:
            result = revoke_api_key(
                conn,
                key_row.id,
                reason=reason,
                actor=get_cli_actor(),
                today=today,
            )
        except ValueError as exc:
            if str(exc) == "already revoked":
                render_error(
                    f"Key already revoked: {_truncate_ident(key_prefix)}",
                    _REVOKE_KEY_ALREADY_HINT,
                )
                raise typer.Exit(code=3) from None
            raise
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    assert result is not None

    # 7. Structured INFO log — NEVER the reason text (may carry
    # customer-specific narrative; FR37/NFR15).
    with contextlib.suppress(Exception):
        _logger.info(
            "Key revoked | id=%s | key_prefix=%s",
            result.key.id,
            result.key.key_prefix,
        )

    # 8. ConfirmationLine. The reason is echoed HERE (operator's own
    # terminal) but NOT in the log — FR37 covers log/audit leakage, not
    # terminal display of a string the operator just typed.
    render_confirmation(
        f"Revoked: {result.key.key_prefix} "
        f"({result.customer_name}) \u2014 reason: {reason}"
    )
