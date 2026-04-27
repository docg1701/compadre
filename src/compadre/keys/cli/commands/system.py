"""System verbs for ``compadre-keys``.

Commands: ``cache-status``, ``suspend-sweep``, ``health``,
``rotate-encryption-key``.

Story 2.3 landed ``cache-status``; Story 3.3 added ``suspend-sweep``;
Story 3.4 added ``health``; Story 4.2 adds ``rotate-encryption-key``.
Grouping the operational verbs in one module keeps the CLI map
legible (customers → keys → billing → system).

Exit-code contract (UX-DR10, matches sibling command modules):
  0 — success (including "no running API instance" for ``cache-status``,
      zero-row sweeps for ``suspend-sweep``, ``health --format table``
      regardless of severity — read-tier semantics; yellow warnings are
      operator-readable hints, not script signals — and zero-active-row
      rotations for ``rotate-encryption-key``).
  2 — usage error (bad ``--format`` on ``cache-status`` / ``health``;
      ``suspend-sweep`` never emits 2 — the only operator-parseable
      flag is ``--dry-run``; operator-aborted confirmation prompt on
      ``rotate-encryption-key``).
  4 — validation error from ``rotate-encryption-key --new-key`` Fernet
      shape check (operator-supplied value is not a valid Fernet key).
  5 — database error from ``get_connection`` / ``_apply_migrations`` /
      ``run_suspension_sweep`` (``suspend-sweep``) / health-collection
      query (``health``) / encryption-rotation decrypt failure on
      ``rotate-encryption-key`` (rolls back atomically — every row
      retains its original ciphertext).
  6 — permission / configuration error (missing encryption key or
      invalid ``BILLING_TIMEZONE``).
  7 — unhealthy state in ``health --format json`` (any check has
      severity ``warn`` or ``fail``). Never emitted by ``cache-status``
      or ``suspend-sweep``; never emitted by ``health --format table``.
"""

from __future__ import annotations

import contextlib
import sqlite3
from datetime import date
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import click
import typer
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr, ValidationError

from compadre.config import get_registry_settings
from compadre.counters import (
    WINDOW_SECONDS,
    aggregate_cache_status,
    read_all_snapshots,
)
from compadre.keys.audit import get_cli_actor
from compadre.keys.cli import _clock
from compadre.keys.cli._errors import first_error_msg, is_missing_encryption_key
from compadre.keys.cli.commands.keys import load_settings_and_tz
from compadre.keys.cli.output import (
    CACHE_STATUS_BOUND_MESSAGE,
    LIST_FORMAT_CHOICES,
    render_cache_status,
    render_confirmation,
    render_empty_state,
    render_error,
    render_health_report,
    render_suspend_sweep_preview,
    stderr_console,
    stdout_console,
)
from compadre.keys.db import (
    _SCHEMA_VERSION,
    _apply_migrations,
    get_connection,
    list_api_keys,
    rotate_encryption_keys,
    run_suspension_sweep,
)
from compadre.keys.health import collect_health_report, health_report_to_json
from compadre.logging import get_logger

_logger = get_logger("keys.cli")

app = typer.Typer(
    help="System observability and operational commands.", no_args_is_help=True
)

_MISSING_KEY_HINT = (
    "Generate one with: python -c 'from cryptography.fernet import "
    "Fernet; print(Fernet.generate_key().decode())' and add it to "
    ".env. Restart the service if monetization is enabled there too."
)

_CACHE_TTL_SECONDS = WINDOW_SECONDS

_ROTATE_KEY_BAD_FERNET_HINT = (
    "Generate a fresh Fernet key with: python -c 'from "
    "cryptography.fernet import Fernet; "
    "print(Fernet.generate_key().decode())'."
)
_ROTATE_KEY_DECRYPT_FAILED_HINT = (
    "The current KEY_REGISTRY_ENCRYPTION_KEY value cannot decrypt at "
    "least one customer's tax-ID ciphertext. The transaction has been "
    "rolled back — every row retains its original encryption. Verify "
    ".env matches the deployment-time key before retrying."
)


@app.command("cache-status")
def cache_status_cmd(
    format: str = typer.Option(
        "table",
        "--format",
        help=(
            "Output format: table (default, human-readable) or json. "
            "Example: --format json."
        ),
    ),
) -> None:
    """Report the auth-cache propagation bound + optional live telemetry.

    Example: compadre-keys cache-status --format json
    """
    # 1. Literal flag validation.
    if format not in LIST_FORMAT_CHOICES:
        render_error(
            "Usage error: --format must be table or json",
            "Valid values: table, json.",
        )
        raise typer.Exit(code=2)

    # 2. Settings + tz validation (exit 6). Even though this command does
    # not touch the DB, the encryption-key presence check keeps the error
    # surface consistent across every ``compadre-keys`` verb — operators
    # don't have to remember which commands require which env vars.
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
        raise typer.Exit(code=6) from None
    except (OSError, PermissionError) as exc:
        render_error("Configuration error: settings file unreadable", str(exc))
        raise typer.Exit(code=6) from None

    if settings.key_registry_encryption_key is None:
        render_error(
            "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
            _MISSING_KEY_HINT,
        )
        raise typer.Exit(code=6)

    try:
        ZoneInfo(settings.billing_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        render_error(
            "Configuration error: BILLING_TIMEZONE is not a valid IANA timezone",
            f"Set BILLING_TIMEZONE to a valid zone (e.g., America/Sao_Paulo). "
            f"Got: {settings.billing_timezone}",
        )
        raise typer.Exit(code=6) from None

    # 3. Read every per-replica snapshot and aggregate. ``read_all_snapshots``
    # globs ``{db_path}.counters.*.json`` so a multi-replica deployment
    # produces a single unified view rather than showing whichever replica
    # wrote last. An absent-file and a parse-error both degrade to "this
    # replica not reporting" without raising.
    snapshots = read_all_snapshots(settings.key_registry_db_path)
    status = aggregate_cache_status(snapshots)

    _logger.info(
        "Cache-status inspected | replicas=%d | entries=%s | freshest_age_s=%s",
        status.replicas if status is not None else 0,
        status.entries if status is not None else None,
        status.snapshot_age_seconds if status is not None else None,
    )

    # 4. Render branch. JSON always carries ``ttl_seconds`` + the verbatim
    # propagation_bound_message (AC9, AC10); table defers to the helper.
    if format == "json":
        payload: dict[str, Any] = {
            "ttl_seconds": _CACHE_TTL_SECONDS,
            "propagation_bound_message": CACHE_STATUS_BOUND_MESSAGE,
            "snapshot_available": status is not None,
        }
        if status is not None:
            payload.update(
                {
                    "replicas": status.replicas,
                    "snapshot_age_seconds": status.snapshot_age_seconds,
                    "entries": status.entries,
                    "maxsize_per_replica": status.maxsize_per_replica,
                    "hits": status.hits,
                    "misses": status.misses,
                    "hit_rate": status.hit_rate,
                    "process_uptime_seconds": status.process_uptime_seconds,
                }
            )
        stdout_console().print_json(data=payload)
        return

    render_cache_status(status)


@app.command("suspend-sweep")
def suspend_sweep_cmd(
    dry_run: bool = typer.Option(
        False,
        "--dry-run",
        help=(
            "Preview the sweep without writing: lists the keys that would "
            "be suspended (prefix + customer + price + paid_until), does "
            "NOT UPDATE, does NOT audit. Exit 0 regardless of count."
        ),
    ),
) -> None:
    """Suspend manual-mode keys whose paid_until has elapsed (FR18 / FR43).

    Designed for cron (00:30 operator-TZ). Safe to run manually to recover
    from a failed cron or to preview the sweep via --dry-run.

    Complimentary keys are never touched (billing_mode='manual' filter).
    Trial-window keys (NULL paid_until) are never touched — those belong
    to the J1 first-sale flow, not the Day-06 enforcement cycle.

    Example: compadre-keys suspend-sweep
             compadre-keys suspend-sweep --dry-run
    """
    # 1. Settings + TZ (exit 6).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, billing_tz = loaded

    # 2. Resolve today in operator TZ. Critical for FR18: the cron runs at
    # 00:30 São Paulo time but the VPS host clock is UTC. Without the
    # TZ-aware resolution, a sweep triggered near midnight could sweep
    # the wrong day's set.
    today = _clock.resolve_today_in_tz(billing_tz)

    # 3. Open DB + migrations (exit 5).
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from exc

    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None

        if dry_run:
            _suspend_sweep_dry_run(conn, today)
            return

        try:
            result = run_suspension_sweep(
                conn, today=today, actor="cron:suspension-sweep"
            )
        except (sqlite3.Error, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from None
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # 4. Structured INFO log — PII-austere aggregate (NFR15).
    with contextlib.suppress(Exception):
        _logger.info(
            "Suspension sweep completed | cutoff=%s | swept_count=%d | "
            "completed_at=%s | dry_run=False",
            result.cutoff_date,
            result.swept_count,
            result.completed_at,
        )

    # 5. ConfirmationLine (UX-DR7).
    render_confirmation(
        f"Suspension sweep completed: {result.swept_count} key(s) suspended "
        f"at {result.completed_at} (cutoff {result.cutoff_date})."
    )


def _suspend_sweep_dry_run(conn: sqlite3.Connection, today: date) -> None:
    """Preview the sweep without writing. No audit rows, no UPDATE, no sentinel.

    Filters ``active`` / ``manual`` rows in Python: non-NULL ``paid_until``
    AND ``paid_until < today``. Sort order matches the at-risk / revenue
    renderers: ``(paid_until ASC, customer_name ASC, key_prefix ASC)``.
    """
    all_active_manual = list_api_keys(conn, status="active", billing_mode="manual")

    preview: list[Any] = []
    for row in all_active_manual:
        if row.paid_until is None:
            continue
        try:
            paid_date = date.fromisoformat(row.paid_until)
        except ValueError:
            # Malformed ``paid_until`` on an active manual row shouldn't happen
            # (``mark-paid`` canonicalises to YYYY-MM-DD on write), but if it
            # does, the real-run SQL lex-compare and the dry-run Python parse
            # diverge. Log a WARN with the key prefix so the operator can see
            # the row they're silently filtering out of the preview.
            with contextlib.suppress(Exception):
                _logger.warning(
                    "Dropped row from dry-run preview: malformed paid_until | "
                    "key_prefix=%s | paid_until=%r",
                    row.key_prefix,
                    row.paid_until,
                )
            continue
        if paid_date < today:
            preview.append(row)

    # ``paid_until is None`` is already filtered above, so the sort key is
    # safely non-optional — no fallback needed.
    preview.sort(key=lambda r: (r.paid_until, r.customer_name, r.key_prefix))

    today_iso = today.isoformat()

    with contextlib.suppress(Exception):
        _logger.info(
            "Suspension sweep completed | cutoff=%s | swept_count=%d | "
            "completed_at=%s | dry_run=True",
            today_iso,
            len(preview),
            "(dry-run)",
        )

    if not preview:
        render_empty_state([f"No keys would be suspended by the sweep on {today_iso}."])
        return

    render_suspend_sweep_preview(preview, today)


@app.command("health")
def health_cmd(
    format: str = typer.Option(
        "table",
        "--format",
        help=(
            "Output format: table (default, human-readable single-border "
            "panel — always exits 0; warnings render as yellow text but "
            "do not flip the exit code) or json (machine-readable; exits "
            "7 when any check has severity warn or fail). Example: "
            "--format json."
        ),
    ),
) -> None:
    """Report registry / cron / cache health (FR34).

    Read-only. Safe to run from cron, monitoring scripts, or the
    operator's terminal. Pipe ``--format json`` into a Nagios-class
    monitor; ``$?`` is 0 for healthy, 7 for any unhealthy condition.

    Example: compadre-keys health
             compadre-keys health --format json
    """
    # 1. Literal flag check (exit 2).
    if format not in LIST_FORMAT_CHOICES:
        render_error(
            "Usage error: --format must be table or json",
            "Valid values: table, json.",
        )
        raise typer.Exit(code=2)

    # 2. Settings + TZ (exit 6). Reuse the shared ladder.
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, _billing_tz = loaded

    # 3. Open DB + migrations (exit 5). ``from exc`` preserves the
    # original traceback per Story 3.3 code-review patch contract.
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from exc

    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from exc

        # 4. Resolve marker / backup paths and collect.
        db_path = settings.key_registry_db_path
        cron_marker_path = f"{db_path}.cron-installed"
        backup_parent = str(Path(db_path).parent)
        try:
            report = collect_health_report(
                conn=conn,
                db_path=db_path,
                schema_version_expected=_SCHEMA_VERSION,
                cron_marker_path=cron_marker_path,
                backup_parent=backup_parent,
                runtime_billing_tz=settings.billing_timezone,
            )
        except (sqlite3.Error, OSError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from exc
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # 5. PII-austere INFO log — counts only (NFR15).
    with contextlib.suppress(Exception):
        _logger.info(
            "Health report generated | format=%s | is_healthy=%s | "
            "warn_count=%d | fail_count=%d",
            format,
            report.is_healthy,
            sum(1 for c in report.checks if c.severity == "warn"),
            sum(1 for c in report.checks if c.severity == "fail"),
        )

    # 6. Render branch.
    if format == "json":
        stdout_console().print_json(data=health_report_to_json(report))
        if not report.is_healthy:
            raise typer.Exit(code=7)
        return

    render_health_report(report)


@app.command("rotate-encryption-key")
def rotate_encryption_key_cmd(
    new_key: str = typer.Option(
        ...,
        "--new-key",
        help=(
            "New Fernet key (44-char base64) to re-encrypt every active "
            "customer's tax ID with. Generate with: python -c 'from "
            "cryptography.fernet import Fernet; "
            "print(Fernet.generate_key().decode())'."
        ),
    ),
    yes: bool = typer.Option(
        False,
        "--yes",
        help="Skip the interactive confirmation prompt (for scripting).",
    ),
) -> None:
    """Re-encrypt every active customer's tax ID under a new Fernet key (FR36).

    Atomic: either every active row's ciphertext flips to the new key OR
    none does. After success, edit .env to set KEY_REGISTRY_ENCRYPTION_KEY
    to the new value and restart the service.

    Example: compadre-keys rotate-encryption-key --new-key gAAAAA...
             compadre-keys rotate-encryption-key --new-key gAAAAA... --yes
    """
    # 1. New-key Fernet shape validation (exit 4). Fail fast on operator
    # typo BEFORE loading settings or opening the DB. Never echo the
    # invalid value back to the operator (NFR16) — they can reconstruct
    # it from shell history if they need to.
    try:
        Fernet(new_key.encode())
    except (ValueError, TypeError):
        render_error(
            "Invalid encryption key: --new-key is not a valid Fernet key",
            _ROTATE_KEY_BAD_FERNET_HINT,
        )
        raise typer.Exit(code=4) from None

    # 2. Settings ladder (exit 6). The billing tz is not consumed by this
    # verb but the shared helper enforces ``KEY_REGISTRY_ENCRYPTION_KEY``
    # presence (the OLD key we need for the decrypt half).
    loaded = load_settings_and_tz()
    if loaded is None:
        raise typer.Exit(code=6)
    settings, _billing_tz = loaded

    # 3. Open DB + migrations (exit 5). ``from exc`` preserves the
    # original traceback per the Story 3.3 code-review patch contract.
    try:
        conn = get_connection(settings.key_registry_db_path)
    except (sqlite3.Error, OSError, RuntimeError) as exc:
        render_error("Database error", str(exc))
        raise typer.Exit(code=5) from exc

    try:
        try:
            _apply_migrations(conn)
        except (sqlite3.Error, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from exc

        # 4. Pre-flight count for the confirmation prompt — so the
        # operator sees scope BEFORE committing. The race window between
        # this count and the helper's materialisation is a non-issue on
        # a single-writer CLI (the helper's count is the audit source
        # of truth).
        try:
            pre_count = conn.execute(
                "SELECT COUNT(*) FROM customer WHERE deleted_at IS NULL"
            ).fetchone()[0]
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from exc

        # 5. Confirmation gate (UX-DR11). Identifies the target DB path so
        # operators running staging + prod registries cannot misdirect.
        if not yes:
            try:
                typer.confirm(
                    (
                        f"This will re-encrypt {pre_count} active customer "
                        f"tax ID(s) in {settings.key_registry_db_path} "
                        f"with the new key. Continue?"
                    ),
                    default=False,
                    abort=True,
                    err=True,
                )
            except click.exceptions.Abort:
                stderr_console().print("Aborted.")
                raise typer.Exit(code=2) from None
            except KeyboardInterrupt:
                stderr_console().print("Aborted.")
                raise typer.Exit(code=130) from None

        # 6. Helper call — atomic. ``InvalidToken`` rolls back; every
        # row retains its original ciphertext (FR36). The ``is None``
        # check is a plain conditional (not ``assert``) so the
        # exit-6 contract survives ``python -O`` / ``PYTHONOPTIMIZE``.
        if settings.key_registry_encryption_key is None:
            render_error(
                "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set",
                "Set KEY_REGISTRY_ENCRYPTION_KEY in .env before rotation.",
            )
            raise typer.Exit(code=6)
        try:
            result = rotate_encryption_keys(
                conn,
                old_key=settings.key_registry_encryption_key,
                new_key=SecretStr(new_key),
                actor=get_cli_actor(),
            )
        except InvalidToken:
            render_error(
                "Database error: tax-ID ciphertext failed to decrypt during rotation",
                _ROTATE_KEY_DECRYPT_FAILED_HINT,
            )
            raise typer.Exit(code=5) from None
        except (sqlite3.Error, OSError, RuntimeError) as exc:
            render_error("Database error", str(exc))
            raise typer.Exit(code=5) from exc
        except KeyboardInterrupt:
            # Ctrl-C during the rotation loop: the helper's ``with
            # conn:`` has already rolled the transaction back; every
            # row retains its original ciphertext.
            stderr_console().print("Aborted.")
            raise typer.Exit(code=130) from None
        except Exception as exc:
            # Defensive catch for unexpected exceptions (TypeError,
            # ValueError from audit guard, MemoryError, etc.) so
            # operators never see a raw traceback. The helper's
            # ``with conn:`` has already rolled back on any exception.
            render_error("Unexpected rotation failure", str(exc))
            raise typer.Exit(code=5) from exc
    finally:
        with contextlib.suppress(sqlite3.Error):
            conn.close()

    # 7. PII-austere INFO log (NFR15). Never the new key, never the
    # old key, never any decrypted tax ID — only structural counts.
    _logger.info(
        "Encryption key rotated | rows_re_encrypted=%d | occurred_at=%s",
        result.rows_re_encrypted,
        result.occurred_at,
    )

    # 8. Success ConfirmationLine (UX-DR7). The new key value IS echoed
    # back — epic AC verbatim — as a copy-paste hint for the .env swap.
    # This is the ONLY place the CLI prints an encryption key value
    # (never in INFO logs, never in error panels, never in audit rows).
    # ``soft_wrap=True`` prevents Rich from breaking the 44-char Fernet
    # key across terminal-width boundaries — a wrapped key pastes into
    # ``.env`` with embedded whitespace and corrupts the env var.
    stdout_console().print(
        f"Encryption key rotated. Update KEY_REGISTRY_ENCRYPTION_KEY in "
        f".env to: {new_key}",
        soft_wrap=True,
    )
