"""`compadre-keys` billing verbs: ``mark-paid`` / ``suspend`` / ``reactivate``
(Story 3.1) and ``at-risk`` / ``revenue`` (Story 3.2).

Exit-code contract (UX-DR10):
  0 — success (also: ``mark-paid`` on a complimentary key — informational,
      no mutation; see AC5 rationale — and empty-result ``at-risk`` /
      ``revenue``; empty results are not errors)
  2 — usage error (bad ``--until`` / ``--window`` / ``--month`` /
      ``--format`` shape, customer-ID passed as key arg)
  3 — not-found / terminal state (key missing, ambiguous prefix,
      already-active / already-suspended / revoked). ``at-risk`` /
      ``revenue`` never emit 3 — they operate on the whole row set and
      do not resolve a single key identifier.
  5 — database error (``sqlite3.Error``, ``RuntimeError`` from helper)
  6 — permission / configuration error (missing encryption key,
      invalid IANA timezone)
"""

from __future__ import annotations

import calendar
import contextlib
import math
import re
import sqlite3
from collections.abc import Iterable
from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

import typer
from dateutil.relativedelta import (  # type: ignore[import-untyped]
    relativedelta,
)

from compadre.keys.audit import get_cli_actor
from compadre.keys.billing import first_charge_date
from compadre.keys.cli import _clock
from compadre.keys.cli.commands.keys import (
    _key_row_to_json,
    _truncate_ident,
    load_settings_and_tz,
    resolve_key_target,
)
from compadre.keys.cli.output import (
    LIST_FORMAT_CHOICES,
    render_at_risk_table,
    render_confirmation,
    render_empty_state,
    render_error,
    render_json,
    render_revenue_report,
    stdout_console,
)
from compadre.keys.db import (
    _apply_migrations,
    earliest_audit_date,
    get_connection,
    list_api_keys,
    mark_key_paid,
    reactivate_key,
    reconstruct_key_state_at,
    suspend_key,
)
from compadre.keys.models import ApiKeyListRow, RevenueReport
from compadre.logging import get_logger

_logger = get_logger("keys.cli")

app = typer.Typer(help="Billing verbs for API keys.", no_args_is_help=True)

# Hints for the three new verbs. Kept here (not in keys.py) because they are
# verb-specific phrasing; the shared identifier hints (``_KEY_NOT_FOUND_HINT``
# etc.) live in keys.py and are imported above.
_MARK_PAID_UNTIL_HINT = (
    "Example: --until 2026-06-05. Alternate ISO forms (e.g. 20260605) are "
    "accepted and normalized."
)
_MARK_PAID_MUTEX_HINT = (
    "Provide exactly one of --until <ISO date> OR --months <N>, not both."
)
_MARK_PAID_NEITHER_HINT = "Provide one of --until <ISO date> or --months <N> (1..36)."
_MARK_PAID_MONTHS_RANGE_HINT = "Months must be an integer between 1 and 36."
_MARK_PAID_PRICE_RANGE_HINT = (
    "Price must be > 0.00 and ≤ 99999.99 (rounded to two decimals)."
)
_MARK_PAID_MONTHS_MAX = 36
_MARK_PAID_PRICE_MAX = Decimal("99999.99")
_MARK_PAID_REVOKED_HINT = (
    "Revoked keys are terminal. Issue a new key via issue-key if the customer returns."
)
_MARK_PAID_COMPLIMENTARY_HINT = (
    "Complimentary keys are bundled externally — no payment tracking "
    "applies. Use update-key to change rate limits."
)
_SUSPEND_ALREADY_HINT = "Run 'compadre-keys show <prefix>' to inspect current status."
_SUSPEND_REVOKED_HINT = (
    "Revoked keys are terminal. Issue a new key via issue-key if the customer returns."
)
_REACTIVATE_ALREADY_HINT = (
    "Run 'compadre-keys show <prefix>' to inspect current status."
)
_REACTIVATE_REVOKED_HINT = (
    "Revoked keys cannot be reactivated. Issue a new key via issue-key if "
    "the customer returns."
)


def _parse_until(raw: str) -> date:
    """Parse ``--until`` and exit 2 on any shape violation.

    Accepts any form ``date.fromisoformat`` parses (``2026-06-05``,
    ``20260605``, etc.) and the caller re-emits ``.isoformat()`` so the
    stored ``paid_until`` is always the canonical ``YYYY-MM-DD`` form —
    non-canonical forms would silently break the lexicographic
    ``paid_until < ?`` comparison in the cron sweep.

    Past dates are accepted — retroactive payment logging is a legitimate
    operator workflow (Story 3.1 AC8). The cron sweep will re-suspend the
    row on the next run if the date is in the past.
    """
    trimmed = raw.strip()
    if not trimmed:
        render_error(
            "Usage error: --until must be an ISO date (YYYY-MM-DD)",
            _MARK_PAID_UNTIL_HINT,
        )
        raise typer.Exit(code=2)
    try:
        return date.fromisoformat(trimmed)
    except ValueError:
        render_error(
            "Usage error: --until must be an ISO date (YYYY-MM-DD)",
            _MARK_PAID_UNTIL_HINT,
        )
        raise typer.Exit(code=2) from None


def _quantize_price(raw: float) -> Decimal:
    """Validate + round a --price-monthly input to two decimals (BRL retail
    half-up rounding). Exits 2 on out-of-range / NaN / inf input.

    BRL retail / NF-e invoice convention is ROUND_HALF_UP (``100.005`` →
    ``100.01``), not banker's rounding. Brazilian-product alignment.
    """
    if math.isnan(raw) or math.isinf(raw):
        render_error(
            "Usage error: --price-monthly must be a finite number",
            _MARK_PAID_PRICE_RANGE_HINT,
        )
        raise typer.Exit(code=2)
    try:
        # Round-trip via str() so the operator's "99.90" doesn't surface as
        # 99.9000000004 from float repr.
        quantized = Decimal(str(raw)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    except InvalidOperation:
        render_error(
            "Usage error: --price-monthly must be a finite number",
            _MARK_PAID_PRICE_RANGE_HINT,
        )
        raise typer.Exit(code=2) from None
    if quantized <= Decimal("0.00") or quantized > _MARK_PAID_PRICE_MAX:
        render_error(
            "Usage error: --price-monthly out of range",
            _MARK_PAID_PRICE_RANGE_HINT,
        )
        raise typer.Exit(code=2)
    return quantized


@app.command("mark-paid")
def mark_paid_cmd(
    key_prefix: str = typer.Argument(
        ...,
        metavar="KEY_PREFIX",
        help=(
            "8-char key prefix, key_<ulid>, or pasted cmpd_live_<plaintext>. "
            'Example: "Pq_abc12".'
        ),
    ),
    until: str | None = typer.Option(
        None,
        "--until",
        help=(
            "Extend paid_until to this ISO date (YYYY-MM-DD). Past dates "
            "are accepted (retroactive logging). Mutually exclusive with "
            "--months. Example: --until 2026-06-05."
        ),
    ),
    months: int | None = typer.Option(
        None,
        "--months",
        min=1,
        max=_MARK_PAID_MONTHS_MAX,
        help=(
            "Extend paid_until by N calendar months (1..36). When the key "
            "is currently paid, anchor = current paid_until; when expired "
            "or unpaid, anchor = today. Month-end anchors clamp to the "
            "target month's last day (Jan 31 + 1 month → Feb 28/29) — use "
            "--until for exact-day renewal. Mutually exclusive with --until. "
            "Example: --months 2."
        ),
    ),
    price_monthly: float | None = typer.Option(
        None,
        "--price-monthly",
        help=(
            "Override the stored monthly price (BRL, 2 decimals, "
            "0.00 < x <= 99999.99). Atomic with the paid_until write. "
            "Example: --price-monthly 99.90."
        ),
    ),
) -> None:
    """Confirm payment for a key and extend its validity (auto-reactivates).

    Example: compadre-keys mark-paid Pq_abc12 --until 2026-06-05
             compadre-keys mark-paid Pq_abc12 --months 2 --price-monthly 99.90
    """
    # 1. Mutex + neither-flag check (exit 2).
    if until is not None and months is not None:
        render_error(
            "Usage error: --until and --months are mutually exclusive",
            _MARK_PAID_MUTEX_HINT,
        )
        raise typer.Exit(code=2)
    if until is None and months is None:
        render_error(
            "Usage error: --until or --months is required",
            _MARK_PAID_NEITHER_HINT,
        )
        raise typer.Exit(code=2)

    # 1b. Pre-validate price (cheapest mistake, no DB needed).
    price_decimal: Decimal | None = None
    if price_monthly is not None:
        price_decimal = _quantize_price(price_monthly)

    # 1c. Pre-canonicalize --until (only when supplied).
    until_iso_from_flag: str | None = None
    if until is not None:
        until_iso_from_flag = _parse_until(until).isoformat()

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

        # 4. Resolve prefix → KeyDetailRow (exit 3 / 2 on non-key branches).
        key_row = resolve_key_target(conn, key_prefix, today=today)

        # 5. Complimentary short-circuit (exit 0, informational) — AC5.
        if key_row.billing_mode == "complimentary":
            render_confirmation(
                f"Key {key_row.key_prefix} is complimentary — "
                "no payment tracking required."
            )
            return

        # 5b. Resolve --months against current key state. Anchor =
        # ``paid_until`` if it is today-or-later (extend the existing
        # window), else ``today`` (start a new window from now). The
        # complimentary short-circuit above ensures we are on a billing key.
        if months is not None:
            current_paid_until: date | None = None
            if key_row.paid_until:
                try:
                    current_paid_until = date.fromisoformat(key_row.paid_until)
                except ValueError:
                    current_paid_until = None
            anchor = (
                current_paid_until
                if current_paid_until is not None and current_paid_until >= today
                else today
            )
            until_iso = (anchor + relativedelta(months=months)).isoformat()
            audit_extra: dict[str, object] = {"mode": "months", "months": months}
        else:
            assert until_iso_from_flag is not None  # CLI-validated above
            until_iso = until_iso_from_flag
            audit_extra = {"mode": "until"}

        # 6. Helper dispatch.
        try:
            result = mark_key_paid(
                conn,
                key_row.id,
                paid_until=until_iso,
                actor=get_cli_actor(),
                today=today,
                new_price_monthly=(
                    float(price_decimal) if price_decimal is not None else None
                ),
                audit_extra=audit_extra,
            )
        except ValueError as exc:
            msg = str(exc)
            if msg == "cannot mark-paid a revoked key":
                render_error(
                    f"Key is revoked: {_truncate_ident(key_prefix)}",
                    _MARK_PAID_REVOKED_HINT,
                )
                raise typer.Exit(code=3) from None
            if msg == "cannot mark-paid a complimentary key":
                # Defense-in-depth — step 5 catches this first in the CLI.
                render_error(
                    f"Key is complimentary: {_truncate_ident(key_prefix)}",
                    _MARK_PAID_COMPLIMENTARY_HINT,
                )
                raise typer.Exit(code=3) from None
            raise
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # ``result`` is not None — resolve_key_target already confirmed the row.
    # Explicit check (not ``assert``) so ``python -O`` cannot strip it and
    # leave a bare ``AttributeError`` traceback on an impossible race.
    if result is None:
        render_error(
            "Database error",
            "Key vanished between resolve and mark-paid; retry the command.",
        )
        raise typer.Exit(code=5)

    # 7. Structured INFO log — no customer name / email / CPF / CNPJ / plaintext.
    with contextlib.suppress(Exception):
        _logger.info(
            "Marked paid | id=%s | paid_until=%s | reactivated=%s | mode=%s",
            result.key.id,
            until_iso,
            result.reactivated,
            audit_extra["mode"],
        )

    # 8. ConfirmationLine (UX-DR7). U+2192 arrow, optional "(reactivated)",
    # plus extras describing months / price changes when those flags fired.
    extra_parts: list[str] = []
    if months is not None:
        extra_parts.append(f"extended {months} month{'s' if months != 1 else ''}")
    if price_decimal is not None:
        extra_parts.append(f"price R$ {price_decimal:.2f}/mo")
    extras = f" ({', '.join(extra_parts)})" if extra_parts else ""
    suffix = " (reactivated)" if result.reactivated else ""
    render_confirmation(
        f"Marked paid: {result.key.key_prefix} "
        f"({result.key.customer_name}) \u2192 paid until {until_iso}"
        f"{extras}{suffix}"
    )


_SUSPEND_REASON_MAX = 1024


@app.command("suspend")
def suspend_cmd(
    key_prefix: str = typer.Argument(
        ...,
        metavar="KEY_PREFIX",
        help=(
            "8-char key prefix, key_<ulid>, or pasted cmpd_live_<plaintext>. "
            'Example: "Pq_abc12".'
        ),
    ),
    reason: str | None = typer.Option(
        None,
        "--reason",
        help=(
            "Optional free-form reason (<=1024 chars, single line). Recorded "
            "in the audit_log row's details_json so forensia can distinguish "
            "abuse / contract / billing-dispute suspensions. Cron-driven "
            "suspensions (Story 3.3 sweep) are anonymous by design — manual "
            "suspensions benefit from operator context."
        ),
    ),
) -> None:
    """Manually suspend an API key (out-of-cycle; for abuse or contract disputes).

    Example: compadre-keys suspend Pq_abc12 --reason "abuse — repeated 429"
    """
    # B12 — --reason shape validation (mirrors revoke-key validators).
    # When ``--reason`` is omitted the audit row carries ``details_json IS
    # NULL`` (same as the cron sweep), keeping rendering parity for
    # ``show --audit-tail``. When provided, NUL / newline / empty / oversize
    # are rejected with exit 2.
    if reason is not None:
        if "\x00" in reason:
            render_error(
                "Usage error: --reason contains illegal characters",
                "Reason text must not contain NUL bytes.",
            )
            raise typer.Exit(code=2)
        if "\n" in reason or "\r" in reason:
            render_error(
                "Usage error: --reason must be a single line",
                (
                    "Reason text must be a single line. Newlines would forge "
                    "dated entries in the stored audit row."
                ),
            )
            raise typer.Exit(code=2)
        stripped = reason.strip()
        if not stripped:
            render_error(
                "Usage error: --reason must be non-empty",
                (
                    "Provide --reason text with content. Whitespace-only "
                    "reasons are rejected; omit --reason for an anonymous "
                    "suspension."
                ),
            )
            raise typer.Exit(code=2)
        if len(stripped) > _SUSPEND_REASON_MAX:
            render_error(
                f"Usage error: --reason exceeds {_SUSPEND_REASON_MAX} characters",
                f"Trim --reason to {_SUSPEND_REASON_MAX} chars or fewer.",
            )
            raise typer.Exit(code=2)
        reason = stripped

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

    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        # 3. Resolve prefix → KeyDetailRow.
        key_row = resolve_key_target(conn, key_prefix, today=today)

        # 4. Helper dispatch.
        try:
            result = suspend_key(
                conn,
                key_row.id,
                actor=get_cli_actor(),
                today=today,
                reason=reason,
            )
        except ValueError as exc:
            msg = str(exc)
            if msg == "cannot suspend a revoked key":
                render_error(
                    f"Key is revoked: {_truncate_ident(key_prefix)}",
                    _SUSPEND_REVOKED_HINT,
                )
                raise typer.Exit(code=3) from None
            if msg == "already suspended":
                render_error(
                    f"Key already suspended: {_truncate_ident(key_prefix)}",
                    _SUSPEND_ALREADY_HINT,
                )
                raise typer.Exit(code=3) from None
            raise
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # Explicit check (not ``assert``) so ``python -O`` cannot strip it.
    if result is None:
        render_error(
            "Database error",
            "Key vanished between resolve and suspend; retry the command.",
        )
        raise typer.Exit(code=5)

    # 5. Structured INFO log.
    with contextlib.suppress(Exception):
        _logger.info(
            "Suspended | id=%s | key_prefix=%s",
            result.key.id,
            result.key.key_prefix,
        )

    # 6. ConfirmationLine (UX-DR7) — bare form, no suffix.
    render_confirmation(
        f"Suspended: {result.key.key_prefix} ({result.key.customer_name})"
    )


@app.command("reactivate")
def reactivate_cmd(
    key_prefix: str = typer.Argument(
        ...,
        metavar="KEY_PREFIX",
        help=(
            "8-char key prefix, key_<ulid>, or pasted cmpd_live_<plaintext>. "
            'Example: "Pq_abc12".'
        ),
    ),
) -> None:
    """Reactivate a suspended key without extending billing (does NOT set paid_until).

    Example: compadre-keys reactivate Pq_abc12
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

    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        # 3. Resolve prefix → KeyDetailRow.
        key_row = resolve_key_target(conn, key_prefix, today=today)

        # 4. Helper dispatch.
        try:
            result = reactivate_key(
                conn,
                key_row.id,
                actor=get_cli_actor(),
                today=today,
            )
        except ValueError as exc:
            msg = str(exc)
            if msg == "cannot reactivate a revoked key":
                render_error(
                    f"Key is revoked: {_truncate_ident(key_prefix)}",
                    _REACTIVATE_REVOKED_HINT,
                )
                raise typer.Exit(code=3) from None
            if msg == "already active":
                render_error(
                    f"Key already active: {_truncate_ident(key_prefix)}",
                    _REACTIVATE_ALREADY_HINT,
                )
                raise typer.Exit(code=3) from None
            raise
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # Explicit check (not ``assert``) so ``python -O`` cannot strip it.
    if result is None:
        render_error(
            "Database error",
            "Key vanished between resolve and reactivate; retry the command.",
        )
        raise typer.Exit(code=5)

    # 5. Structured INFO log.
    with contextlib.suppress(Exception):
        _logger.info(
            "Reactivated | id=%s | key_prefix=%s",
            result.key.id,
            result.key.key_prefix,
        )

    # 6. ConfirmationLine (UX-DR7) — bare form.
    render_confirmation(
        f"Reactivated: {result.key.key_prefix} ({result.key.customer_name})"
    )


# ---------------------------------------------------------------------------
# Story 3.2 — read-only billing reports: ``at-risk`` and ``revenue``.
# ---------------------------------------------------------------------------

# ``--window 0d`` is deliberately rejected: the "paid_until <= today" query
# belongs to ``list-keys --paid-until-lte today``; at-risk is strictly
# look-ahead. The upper cap prevents the "whole portfolio" degenerate (the
# operator should use ``list-keys`` for that).
_AT_RISK_WINDOW_RE = re.compile(r"^[1-9][0-9]*d$")
_AT_RISK_WINDOW_MAX_DAYS = 365
_AT_RISK_WINDOW_HINT = (
    "Example: --window 5d (today through today+5). Use a larger window "
    "for more look-ahead."
)
_AT_RISK_WINDOW_CAP_HINT = (
    "Maximum window is 365d. Use 'compadre-keys list-keys' for a full scan."
)

# Sane operator bounds — the registry was first migrated in 2026; years
# before 2020 cannot carry legitimate data, years after 2099 are not a
# use case anyone is asking for. The CLI rejects inputs outside this
# window at exit 2 instead of producing a confusing empty report.
_REVENUE_YEAR_MIN = 2020
_REVENUE_YEAR_MAX = 2099
_REVENUE_MONTH_RE = re.compile(r"^[0-9]{4}-[0-9]{2}$")
_REVENUE_MONTH_HINT = (
    "Example: --month current or --month 2026-05. Month must be 'current' "
    f"or YYYY-MM with year in {_REVENUE_YEAR_MIN}..{_REVENUE_YEAR_MAX}."
)

_FORMAT_HINT = "Valid values: table, json."


def _parse_window_days(raw: str) -> int:
    """Parse ``--window`` into an integer day count or exit 2.

    Rejects non-canonical shapes (``-5d``, ``5w``, ``5``, empty, ``1.5d``)
    and the 365-day upper cap. ``0d`` is rejected because the zero-day
    semantic belongs to ``list-keys --paid-until-lte today``.
    """
    trimmed = raw.strip()
    if not _AT_RISK_WINDOW_RE.match(trimmed):
        render_error(
            "Usage error: --window must be Nd where N \u2265 1 (days)",
            _AT_RISK_WINDOW_HINT,
        )
        raise typer.Exit(code=2)
    window_days = int(trimmed[:-1])
    if window_days > _AT_RISK_WINDOW_MAX_DAYS:
        render_error(
            f"Usage error: --window exceeds {_AT_RISK_WINDOW_MAX_DAYS}d cap",
            _AT_RISK_WINDOW_CAP_HINT,
        )
        raise typer.Exit(code=2)
    return window_days


def _parse_month(raw: str, *, today: date) -> tuple[int, int]:
    """Parse ``--month`` into a ``(year, month)`` tuple or exit 2.

    ``current`` resolves against the operator-TZ ``today`` the caller
    has already computed so the two surfaces agree on the month boundary.
    """
    trimmed = raw.strip()
    if trimmed == "current":
        return today.year, today.month
    if not _REVENUE_MONTH_RE.match(trimmed):
        render_error(
            "Usage error: --month must be 'current' or YYYY-MM (e.g., 2026-05)",
            _REVENUE_MONTH_HINT,
        )
        raise typer.Exit(code=2)
    year_str, month_str = trimmed.split("-")
    year = int(year_str)
    month = int(month_str)
    if not _REVENUE_YEAR_MIN <= year <= _REVENUE_YEAR_MAX:
        render_error(
            "Usage error: --month year outside supported range "
            f"({_REVENUE_YEAR_MIN}..{_REVENUE_YEAR_MAX})",
            _REVENUE_MONTH_HINT,
        )
        raise typer.Exit(code=2)
    if not 1 <= month <= 12:
        render_error(
            "Usage error: --month component must be 01..12",
            _REVENUE_MONTH_HINT,
        )
        raise typer.Exit(code=2)
    return year, month


def _at_risk_row_to_json(row: ApiKeyListRow, *, days_left: int) -> dict[str, object]:
    """Serialize an at-risk row to JSON.

    Reuses the Story 1.3 ``_key_row_to_json`` shape then appends
    ``days_left`` so scripted pipelines see the same schema as
    ``list-keys --format json`` plus the at-risk-specific delta.
    """
    payload = _key_row_to_json(row)
    payload["days_left"] = days_left
    return payload


def _revenue_report_to_json(report: RevenueReport) -> None:
    """Emit ``report`` as a single JSON object via the shared stdout console."""
    payload: dict[str, object] = {
        "month": report.month_iso,
        "month_label": report.month_label,
        "paid_count": report.paid_count,
        "pending_count": report.pending_count,
        "suspended_count": report.suspended_count,
        "complimentary_count": report.complimentary_count,
        "mrr": report.mrr,
        "projected": report.projected,
        "next_charge_date": report.next_charge_date,
        "complimentary": [
            {
                "key_prefix": r.key_prefix,
                "customer_name": r.customer_name,
                # B15 — JSON consumers need the same suspended/revoked
                # signal the table renderer surfaces; otherwise dashboards
                # treat all complimentary rows as live.
                "status": r.status,
                "notes": r.notes,
            }
            for r in report.complimentary_rows
        ],
    }
    stdout_console().print_json(data=payload)


def _bucketize_for_revenue(
    rows: Iterable[ApiKeyListRow], *, month_first: date
) -> tuple[
    list[ApiKeyListRow],
    list[ApiKeyListRow],
    list[ApiKeyListRow],
    list[ApiKeyListRow],
]:
    """Single-pass bucketize: (paid, pending, suspended, complimentary).

    Revoked rows are dropped entirely. Complimentary rows bucket on
    billing-mode regardless of status (suspended complimentary still
    counts here — AC10). Manual-mode paid / pending split hinges on the
    calendar-month coverage predicate documented in the story.
    """
    paid: list[ApiKeyListRow] = []
    pending: list[ApiKeyListRow] = []
    suspended: list[ApiKeyListRow] = []
    complimentary: list[ApiKeyListRow] = []
    for row in rows:
        if row.status == "revoked":
            continue
        if row.billing_mode == "complimentary":
            complimentary.append(row)
            continue
        # manual-mode rows from here down.
        if row.status == "suspended":
            suspended.append(row)
            continue
        if row.status != "active":
            # Defensive: future CustomerStatus variants or automated-mode
            # rows shouldn't silently leak into a bucket. Drop rather than
            # raise — keeping revenue robust against data drift.
            continue
        if row.paid_until is None:
            pending.append(row)
            continue
        try:
            paid_date = date.fromisoformat(row.paid_until)
        except ValueError:
            # Canonical ISO dates are enforced at write-time (Story 3.1
            # mark-paid canonicalizes). If a corrupted row slips through,
            # treat it as pending so the operator sees it on the chase list.
            pending.append(row)
            continue
        if paid_date >= month_first:
            paid.append(row)
        else:
            pending.append(row)
    return paid, pending, suspended, complimentary


@app.command("at-risk")
def at_risk_cmd(
    window: str = typer.Option(
        "5d",
        "--window",
        help=(
            "Look-ahead window for expirations. Format: Nd where N is a "
            "positive integer (days). Example: --window 5d, --window 14d."
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
    """Preview active manual keys whose paid_until falls within a window.

    Trial-window keys (active manual rows with ``paid_until IS NULL``)
    are excluded — they belong to the J1 first-sale flow, not the
    Day-04 pre-billing review.

    Example: compadre-keys at-risk --window 5d
    """
    # 1. --format membership.
    if format not in LIST_FORMAT_CHOICES:
        render_error("Usage error: --format must be table or json", _FORMAT_HINT)
        raise typer.Exit(code=2)

    # 2. --window shape + upper cap (exit 2; no DB work on reject).
    window_days = _parse_window_days(window)

    # 3. Settings + TZ (exit 6).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded
    today = _clock.resolve_today_in_tz(billing_tz)
    cutoff = today + timedelta(days=window_days)

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

        # 5. Enumerate the full active-manual set. Trial-window (NULL
        # paid_until) rows are filtered in Python — the SQL
        # paid_until_lte filter matches NULL-OR-lte which is the opposite
        # of at-risk semantics.
        all_active_manual = list_api_keys(
            conn,
            status="active",
            billing_mode="manual",
        )
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # 6. Filter: today <= paid_until <= cutoff. NULL excluded.
    in_window: list[tuple[ApiKeyListRow, int]] = []
    for row in all_active_manual:
        if row.paid_until is None:
            continue
        try:
            paid_date = date.fromisoformat(row.paid_until)
        except ValueError:
            continue
        if today <= paid_date <= cutoff:
            in_window.append((row, (paid_date - today).days))

    # 7. Sort: (paid_until ASC, customer_name ASC, key_prefix ASC). The
    # tertiary key guarantees a deterministic row order when two keys
    # share the same paid_until AND the same customer_name (e.g. one
    # customer with multiple active keys) — table and JSON output stay
    # reproducible regardless of DB insertion order.
    in_window.sort(
        key=lambda pair: (
            pair[0].paid_until or "",
            pair[0].customer_name,
            pair[0].key_prefix,
        )
    )

    # 8. Structured INFO log — PII-austere aggregate.
    with contextlib.suppress(Exception):
        _logger.info(
            "At-risk queried | window_days=%d | count=%d",
            window_days,
            len(in_window),
        )

    # 9. Branch on --format.
    if format == "json":
        render_json(
            [_at_risk_row_to_json(row, days_left=delta) for row, delta in in_window]
        )
        return

    if not in_window:
        render_empty_state([f"No keys at risk in the next {window_days} days."])
        return

    render_at_risk_table(in_window, window_days)


@app.command("revenue")
def revenue_cmd(
    month: str = typer.Option(
        "current",
        "--month",
        help=(
            "Month to report: 'current' or YYYY-MM. Example: --month current "
            "or --month 2026-05."
        ),
    ),
    format: str = typer.Option(
        "table",
        "--format",
        help=(
            "Output format: table (default, human-readable panel) or json. "
            "Example: --month 2026-05 --format json."
        ),
    ),
    as_of: str | None = typer.Option(
        None,
        "--as-of",
        help=(
            "Replay portfolio state as it would have looked on this ISO "
            "date (YYYY-MM-DD), then bucketize against --month. Walks the "
            "audit log to reconstruct status / paid_until / price_monthly "
            "per key. Must be in [earliest_audit_row_date, today]. "
            "Story 5.1 Gap 8. Example: --as-of 2026-04-15."
        ),
    ),
) -> None:
    """Monthly revenue report: paid / pending / suspended / complimentary.

    Shows live portfolio state filtered by the reporting month's
    ``paid_until`` coverage. With ``--as-of YYYY-MM-DD``, replays the audit
    log to reconstruct the state of every key on that date and bucketizes
    that historical snapshot against ``--month`` (Story 5.1 Gap 8).

    Example: compadre-keys revenue --month current
             compadre-keys revenue --month 2026-04 --as-of 2026-04-15
    """
    # 1. --format membership.
    if format not in LIST_FORMAT_CHOICES:
        render_error("Usage error: --format must be table or json", _FORMAT_HINT)
        raise typer.Exit(code=2)

    # 2. Settings + TZ (exit 6). We resolve ``today`` before parsing --month
    # so 'current' is anchored to operator-local time (AC18).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded
    today = _clock.resolve_today_in_tz(billing_tz)

    # 2b. --as-of shape + bounds (exit 2; pre-DB except for the lower-bound
    # check which needs the audit-log MIN — done after the DB opens).
    as_of_date: date | None = None
    if as_of is not None:
        try:
            as_of_date = date.fromisoformat(as_of.strip())
        except ValueError:
            render_error(
                "Usage error: --as-of must be an ISO date (YYYY-MM-DD)",
                "Example: --as-of 2026-04-15.",
            )
            raise typer.Exit(code=2) from None
        if as_of_date > today:
            render_error(
                "Usage error: --as-of cannot be in the future",
                f"Today (in billing TZ) is {today.isoformat()}.",
            )
            raise typer.Exit(code=2)

    # 3. --month shape + bounds (exit 2).
    year, month_int = _parse_month(month, today=today)

    # 4. Month boundaries.
    month_first = date(year, month_int, 1)
    days_in_month = calendar.monthrange(year, month_int)[1]
    month_last = date(year, month_int, days_in_month)
    month_iso = f"{year:04d}-{month_int:02d}"
    # strftime("%B %Y") is locale-independent for ASCII English month
    # names — the project's language policy forbids pt-BR labels here.
    month_label = month_first.strftime("%B %Y")

    # Next charge: the first billing_day strictly after the reporting
    # month's last day. For --month current, today may be after that
    # date's billing_day, but the semantic is "when does THIS month's
    # billing cycle close" — always the next billing_day after
    # month_last (reused from Story 1.2's first_charge_date helper).
    next_charge = first_charge_date(
        today=month_last,
        billing_day=settings.billing_day,
        tz=settings.billing_timezone,
    )

    # 5. Open DB + migrations (exit 5).
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

        # 6. Enumerate the whole row set; bucketize in Python.
        if as_of_date is not None:
            # Lower-bound check needs the audit MIN — do it now that the
            # connection is open. Empty audit_log + --as-of supplied is an
            # operator error (no history to replay).
            earliest = earliest_audit_date(conn, billing_tz)
            if earliest is None:
                render_error(
                    "Usage error: --as-of has nothing to replay",
                    "The audit log is empty.",
                )
                raise typer.Exit(code=2)
            if as_of_date < earliest:
                render_error(
                    "Usage error: --as-of predates the audit log",
                    f"Earliest audit row: {earliest.isoformat()}.",
                )
                raise typer.Exit(code=2)
            all_rows = reconstruct_key_state_at(conn, as_of_date, tz=billing_tz)
        else:
            all_rows = list_api_keys(conn)
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    paid, pending, suspended, complimentary = _bucketize_for_revenue(
        all_rows, month_first=month_first
    )

    mrr = sum(row.price_monthly for row in paid)
    projected = mrr + sum(row.price_monthly for row in pending)

    # Stable display order for the complimentary list — same invariant
    # the at-risk table uses (paid_until ASC, customer_name ASC,
    # key_prefix ASC). Comp rows usually have NULL paid_until so this
    # degenerates to customer_name ordering with key_prefix as the
    # tertiary tie-break for one-customer-multiple-keys deterministic
    # output.
    complimentary_sorted = sorted(
        complimentary,
        key=lambda r: (r.paid_until or "", r.customer_name, r.key_prefix),
    )

    report = RevenueReport(
        month_label=month_label,
        month_iso=month_iso,
        paid_count=len(paid),
        pending_count=len(pending),
        suspended_count=len(suspended),
        complimentary_count=len(complimentary_sorted),
        mrr=mrr,
        projected=projected,
        next_charge_date=next_charge.isoformat(),
        complimentary_rows=tuple(complimentary_sorted),
    )

    # 7. Structured INFO log — PII-austere aggregate.
    with contextlib.suppress(Exception):
        _logger.info(
            "Revenue report | month=%s | paid=%d | pending=%d | suspended=%d | "
            "complimentary=%d | mrr=%.2f | projected=%.2f",
            report.month_iso,
            report.paid_count,
            report.pending_count,
            report.suspended_count,
            report.complimentary_count,
            report.mrr,
            report.projected,
        )

    # 8. Branch on --format.
    if format == "json":
        _revenue_report_to_json(report)
        return

    all_empty = (
        report.paid_count == 0
        and report.pending_count == 0
        and report.suspended_count == 0
        and report.complimentary_count == 0
    )
    if all_empty:
        render_empty_state([f"No keys to report for {report.month_label}."])
        return

    render_revenue_report(report)
