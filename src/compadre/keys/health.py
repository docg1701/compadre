"""``compadre-keys health`` data-collection module (Story 3.4, FR34).

Computes a ``HealthReport`` from: the SQLite registry, the cron-installed
marker file, the optional backup glob, and the per-replica counter
snapshots. No mutation. Safe to run from cron / monitoring scripts.

Severity model:
  ok    — nominal; renders green; never affects ``is_healthy``.
  info  — informational; no marker; never affects ``is_healthy``.
  warn  — operational drift; renders yellow; flips ``is_healthy=False``
          in JSON mode → exit 7.
  fail  — failed check; renders red; flips ``is_healthy=False`` in JSON
          mode → exit 7. Distinguished from ``warn`` purely by visual
          severity in table mode; equivalent in JSON for aggregation.
"""

from __future__ import annotations

import sqlite3
import stat as stat_mod
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from compadre.counters import (
    AggregatedCacheStatus,
    aggregate_cache_status,
    read_all_snapshots,
)
from compadre.keys.db import (
    count_active_customers,
    count_keys_by_status,
    count_tombstones,
    get_last_sweep_timestamp,
)
from compadre.keys.formatters import format_seconds_ago

Severity = Literal["ok", "info", "warn", "fail"]

# Story 3.4 ↔ Story 4.3 handshake: Story 4.3 shipped the backup playbook
# on 2026-04-20 so this constant flipped from ``True`` to ``False`` —
# ``_check_last_backup`` now emits warn/ok instead of staying at
# ``info`` forever. Kept as a named constant for symmetry with the
# Story 3.4 forcing-function tests and for a quick operator rollback
# surface if the backup playbook is ever regressed.
_BACKUP_NOT_IMPLEMENTED_INFO = False

# The sweep is considered fresh within 48 h (Story 3.4 spec §2). The cron
# fires daily at 00:30 operator-TZ; a 48 h threshold tolerates one missed
# run without flipping to warn, which absorbs ordinary ops noise.
_SWEEP_STALENESS_WARN_SECONDS = 48 * 3600

_ISO_UTC_FORMAT = "%Y-%m-%dT%H:%M:%SZ"


def _utcnow() -> datetime:
    """Clock seam — monkeypatched in tests to freeze time."""
    return datetime.now(UTC)


def _utcnow_iso() -> str:
    return _utcnow().strftime(_ISO_UTC_FORMAT)


@dataclass(frozen=True, slots=True)
class HealthCheck:
    """One field in the report: what was measured + how to interpret it.

    ``label``  — operator-facing title ("Database", "Cron", ...).
    ``value``  — raw renderable string (plain text, no markup).
    ``severity`` — drives colour (table) and ``is_healthy`` (JSON).
    ``detail`` — optional human hint ("expected 0600").
    ``data``   — optional structured payload for the JSON output.
    """

    label: str
    value: str
    severity: Severity
    detail: str | None = None
    data: dict[str, object] | None = None


@dataclass(frozen=True, slots=True)
class HealthReport:
    """Result payload returned by ``collect_health_report``."""

    checks: tuple[HealthCheck, ...]
    is_healthy: bool
    generated_at: str  # canonical %Y-%m-%dT%H:%M:%SZ


def _format_size(size_bytes: int) -> str:
    """Render a byte count as a compact humanized string."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    if size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


def _parse_iso_utc(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, _ISO_UTC_FORMAT).replace(tzinfo=UTC)
    except (ValueError, TypeError):
        return None


def _check_db_path(db_path: Path) -> HealthCheck:
    """Stat the DB file — size + mode. Warns on mode drift from 0o600."""
    if not db_path.exists():
        return HealthCheck(
            label="Database",
            value=f"{db_path} (missing)",
            severity="fail",
            detail="database file does not exist",
            data={
                "path": str(db_path),
                "exists": False,
                "size_bytes": None,
                "mode_octal": None,
            },
        )
    st = db_path.stat()
    mode = stat_mod.S_IMODE(st.st_mode)
    mode_str = f"{mode:04o}"
    size_h = _format_size(st.st_size)
    if mode != 0o600:
        return HealthCheck(
            label="Database",
            value=f"{db_path} ({size_h}, mode {mode_str})",
            severity="warn",
            detail="expected 0600; run chmod 600 on the registry file",
            data={
                "path": str(db_path),
                "exists": True,
                "size_bytes": int(st.st_size),
                "mode_octal": mode_str,
            },
        )
    return HealthCheck(
        label="Database",
        value=f"{db_path} ({size_h}, mode {mode_str})",
        severity="ok",
        detail=None,
        data={
            "path": str(db_path),
            "exists": True,
            "size_bytes": int(st.st_size),
            "mode_octal": mode_str,
        },
    )


def _check_schema_version(conn: sqlite3.Connection, *, expected: int) -> HealthCheck:
    """Compare on-disk schema version to the binary's expected version."""
    row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
    current = int(row[0]) if row is not None and row[0] is not None else 0
    if current == expected:
        return HealthCheck(
            label="Schema version",
            value=f"{current} (current)",
            severity="ok",
            detail=None,
            data={"current": current, "expected": expected},
        )
    if current < expected:
        return HealthCheck(
            label="Schema version",
            value=f"{current} (migration pending, expected {expected})",
            severity="warn",
            detail=("run any other compadre-keys command to bootstrap migrations"),
            data={"current": current, "expected": expected},
        )
    return HealthCheck(
        label="Schema version",
        value=f"{current} (binary too old, expected {expected})",
        severity="fail",
        detail="upgrade compadre before running further commands",
        data={"current": current, "expected": expected},
    )


def _check_customer_count(conn: sqlite3.Connection) -> HealthCheck:
    active = count_active_customers(conn)
    tombstoned = count_tombstones(conn)
    if tombstoned == 0:
        value = f"{active} active"
    else:
        value = f"{active} active, {tombstoned} tombstoned"
    return HealthCheck(
        label="Customers",
        value=value,
        severity="ok",
        detail=None,
        data={"active": active, "tombstoned": tombstoned},
    )


def _check_key_counts(conn: sqlite3.Connection) -> HealthCheck:
    counts = count_keys_by_status(conn)
    value = (
        f"{counts['active']} active, "
        f"{counts['suspended']} suspended, "
        f"{counts['revoked']} revoked"
    )
    return HealthCheck(
        label="Keys",
        value=value,
        severity="ok",
        detail=None,
        data={
            "active": counts["active"],
            "suspended": counts["suspended"],
            "revoked": counts["revoked"],
        },
    )


def _check_last_sweep(
    conn: sqlite3.Connection,
    *,
    cron_installed: bool,
    staleness_warn_seconds: int,
    cron_install_age_seconds: int | None = None,
) -> HealthCheck:
    last_at = get_last_sweep_timestamp(conn)
    if last_at is None:
        if cron_installed:
            # Grace period: a freshly installed cron that hasn't fired yet
            # is the legitimate post-bootstrap state, not operational drift.
            # Report info (which doesn't flip is_healthy) until the cron
            # has been installed long enough that it should have fired.
            # Beyond the staleness threshold the absence is genuine drift
            # (cron daemon stopped, suspended host, mounted-over crontab) →
            # warn.
            within_grace = (
                cron_install_age_seconds is not None
                and cron_install_age_seconds < staleness_warn_seconds
            )
            if within_grace:
                return HealthCheck(
                    label="Last suspension sweep",
                    value="never (cron freshly installed)",
                    severity="info",
                    detail="awaiting first 00:30 operator-TZ firing",
                    data={
                        "last_at": None,
                        "age_seconds": None,
                        "staleness_warn_seconds": staleness_warn_seconds,
                        "cron_install_age_seconds": cron_install_age_seconds,
                    },
                )
            return HealthCheck(
                label="Last suspension sweep",
                value="never (cron installed but not yet fired)",
                severity="warn",
                detail="cron should have fired by now — check cron daemon",
                data={
                    "last_at": None,
                    "age_seconds": None,
                    "staleness_warn_seconds": staleness_warn_seconds,
                    "cron_install_age_seconds": cron_install_age_seconds,
                },
            )
        return HealthCheck(
            label="Last suspension sweep",
            value="never (cron not installed)",
            severity="info",
            detail=None,
            data={
                "last_at": None,
                "age_seconds": None,
                "staleness_warn_seconds": staleness_warn_seconds,
            },
        )
    parsed = _parse_iso_utc(last_at)
    if parsed is None:
        return HealthCheck(
            label="Last suspension sweep",
            value=f"{last_at} (unparseable timestamp)",
            severity="warn",
            detail="audit_log row has a malformed occurred_at",
            data={
                "last_at": last_at,
                "age_seconds": None,
                "staleness_warn_seconds": staleness_warn_seconds,
            },
        )
    delta = int((_utcnow() - parsed).total_seconds())
    age_label = format_seconds_ago(delta)
    # Future timestamp — clock skew between host and container, audit-log
    # tamper, or a TZ misconfig writing local time as UTC. Surface as warn
    # rather than letting format_seconds_ago's negative clamp render it
    # as a healthy "0s ago".
    if delta < 0:
        return HealthCheck(
            label="Last suspension sweep",
            value=f"{last_at} (future timestamp)",
            severity="warn",
            detail="timestamp is in the future — check clock skew or TZ",
            data={
                "last_at": last_at,
                "age_seconds": delta,
                "staleness_warn_seconds": staleness_warn_seconds,
            },
        )
    if delta > staleness_warn_seconds:
        hours = staleness_warn_seconds // 3600
        return HealthCheck(
            label="Last suspension sweep",
            value=f"{last_at} ({age_label})",
            severity="warn",
            detail=f"expected within {hours}h — check cron daemon",
            data={
                "last_at": last_at,
                "age_seconds": delta,
                "staleness_warn_seconds": staleness_warn_seconds,
            },
        )
    return HealthCheck(
        label="Last suspension sweep",
        value=f"{last_at} ({age_label})",
        severity="ok",
        detail=None,
        data={
            "last_at": last_at,
            "age_seconds": delta,
            "staleness_warn_seconds": staleness_warn_seconds,
        },
    )


def _marker_parse(text: str) -> dict[str, str] | None:
    """Parse the cron-installed marker. Returns ``None`` when unparseable.

    Marker format: ``key=value`` lines, ASCII. Unknown keys are tolerated
    silently so a future schema extension (Story 4.x) stays
    forward-compatible. A parse failure anywhere returns ``None`` so the
    renderer can surface a single "unparseable" warn path.
    """
    result: dict[str, str] = {}
    had_kv = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        # Binary / control byte in the line → treat as unparseable.
        if any(ord(ch) < 0x20 and ch not in "\t" for ch in line):
            return None
        if "=" not in line:
            return None
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if not key:
            return None
        result[key] = value
        had_kv = True
    if not had_kv:
        return None
    return result


def _check_cron_marker(marker_path: Path) -> HealthCheck:
    if not marker_path.exists():
        return HealthCheck(
            label="Cron",
            value="Not installed",
            severity="warn",
            detail="run ansible-playbook bootstrap.yml",
            data={"installed": False},
        )
    try:
        text = marker_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return HealthCheck(
            label="Cron",
            value="Marker present but unparseable",
            severity="warn",
            detail="re-run ansible-playbook bootstrap.yml",
            data={"installed": True, "parsed": False},
        )
    parsed = _marker_parse(text)
    if parsed is None:
        return HealthCheck(
            label="Cron",
            value="Marker present but unparseable",
            severity="warn",
            detail="re-run ansible-playbook bootstrap.yml",
            data={"installed": True, "parsed": False},
        )
    schedule = parsed.get("schedule", "").strip()
    cron_tz = parsed.get("cron_tz", "").strip()
    if not schedule or not cron_tz:
        return HealthCheck(
            label="Cron",
            value="Marker present but unparseable",
            severity="warn",
            detail="re-run ansible-playbook bootstrap.yml",
            data={"installed": True, "parsed": False},
        )
    value = f"compadre-daily-suspension at {schedule} (CRON_TZ={cron_tz})"
    # Marker mtime ≈ install time (set by bootstrap.yml's ``cat > marker``).
    # Surfaced so freshness probes (_check_last_sweep / _check_last_backup)
    # can grant a grace period: a "never fired yet" reading inside one
    # cron interval after install is the legitimate post-bootstrap state,
    # not operational drift.
    install_age = _marker_install_age_seconds(marker_path)
    return HealthCheck(
        label="Cron",
        value=value,
        severity="ok",
        detail=None,
        data={
            "installed": True,
            "schedule": schedule,
            "tz": cron_tz,
            "install_age_seconds": install_age,
        },
    )


def _marker_install_age_seconds(marker_path: Path) -> int | None:
    """Return seconds since the marker file was written, or None on stat failure."""
    try:
        mtime = marker_path.stat().st_mtime
    except OSError:
        return None
    return int(_utcnow().timestamp() - mtime)


def _check_billing_timezone_drift(
    cron_check: HealthCheck,
    backup_cron_check: HealthCheck,
    *,
    runtime_billing_tz: str,
) -> HealthCheck:
    """Cross-validate marker ``cron_tz`` against runtime ``BILLING_TIMEZONE``.

    The CLAUDE.md gotcha: drift between the cron's ``CRON_TZ`` (set by
    ``bootstrap.yml`` from ``billing_timezone`` at install time) and the
    app's runtime ``BILLING_TIMEZONE`` env var silently shifts the app's
    "today" relative to the cron schedule — suspensions fire at the wrong
    operator-local time. This check surfaces the drift loudly.

    Severity model:
      - info: no marker present (cron not installed) — caller already
        warned via the Cron row; no need to double-warn.
      - ok:   both markers' tz match the runtime tz.
      - warn: any marker's tz disagrees with the runtime tz.
    """
    cron_tz = (cron_check.data or {}).get("tz")
    backup_tz = (backup_cron_check.data or {}).get("tz")
    observed: list[tuple[str, str]] = []
    if isinstance(cron_tz, str) and cron_tz:
        observed.append(("suspension cron", cron_tz))
    if isinstance(backup_tz, str) and backup_tz:
        observed.append(("backup cron", backup_tz))
    if not observed:
        return HealthCheck(
            label="Timezone drift",
            value="No cron markers — nothing to compare",
            severity="info",
            detail=None,
            data={
                "runtime": runtime_billing_tz,
                "suspension_cron_tz": cron_tz if isinstance(cron_tz, str) else None,
                "backup_cron_tz": backup_tz if isinstance(backup_tz, str) else None,
                "matches": None,
            },
        )
    mismatches = [(label, tz) for (label, tz) in observed if tz != runtime_billing_tz]
    if not mismatches:
        return HealthCheck(
            label="Timezone drift",
            value=f"All cron markers agree with BILLING_TIMEZONE={runtime_billing_tz}",
            severity="ok",
            detail=None,
            data={
                "runtime": runtime_billing_tz,
                "suspension_cron_tz": cron_tz if isinstance(cron_tz, str) else None,
                "backup_cron_tz": backup_tz if isinstance(backup_tz, str) else None,
                "matches": True,
            },
        )
    drift_pairs = ", ".join(f"{label}={tz}" for (label, tz) in mismatches)
    return HealthCheck(
        label="Timezone drift",
        value=f"BILLING_TIMEZONE={runtime_billing_tz} but {drift_pairs}",
        severity="warn",
        detail=(
            "re-run ansible-playbook bootstrap.yml after aligning "
            "billing_timezone with the runtime BILLING_TIMEZONE"
        ),
        data={
            "runtime": runtime_billing_tz,
            "suspension_cron_tz": cron_tz if isinstance(cron_tz, str) else None,
            "backup_cron_tz": backup_tz if isinstance(backup_tz, str) else None,
            "matches": False,
        },
    )


def _check_backup_cron_marker(marker_path: Path) -> HealthCheck:
    """Backup-cron marker — written by ``bootstrap.yml`` at install time.

    Marker format (Story 4.3 §3.4):
      schedule=<cron-expression>
      cron_tz=<IANA-timezone>
      retention_days=<positive int>

    Shares parsing with ``_check_cron_marker`` via ``_marker_parse`` and
    mirrors its warn-on-missing / warn-on-unparseable shape. Diverges in
    three ways:
      - adds an ``is_file()`` guard (a directory / symlink / FIFO at the
        marker path would otherwise route to the generic "unparseable"
        branch with a misleading "re-run bootstrap.yml" hint);
      - validates ``retention_days`` is a positive integer (a string like
        ``banana`` or ``-7`` must not render as green
        ``retention=bananad`` / ``retention=-7d``);
      - surfaces ``retention_days`` as an int in the rendered value AND
        in the JSON ``data`` (operators reading the health report need
        the archive-retention horizon alongside schedule + TZ).
    """
    if not marker_path.exists():
        return HealthCheck(
            label="Backup cron",
            value="Not installed",
            severity="warn",
            detail="run ansible-playbook bootstrap.yml",
            data={"installed": False},
        )
    if not marker_path.is_file():
        return HealthCheck(
            label="Backup cron",
            value="Marker path is not a regular file",
            severity="warn",
            detail="remove it and re-run ansible-playbook bootstrap.yml",
            data={"installed": True, "parsed": False, "regular_file": False},
        )
    try:
        text = marker_path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return HealthCheck(
            label="Backup cron",
            value="Marker present but unparseable",
            severity="warn",
            detail="re-run ansible-playbook bootstrap.yml",
            data={"installed": True, "parsed": False},
        )
    parsed = _marker_parse(text)
    if parsed is None:
        return HealthCheck(
            label="Backup cron",
            value="Marker present but unparseable",
            severity="warn",
            detail="re-run ansible-playbook bootstrap.yml",
            data={"installed": True, "parsed": False},
        )
    schedule = parsed.get("schedule", "").strip()
    cron_tz = parsed.get("cron_tz", "").strip()
    retention = parsed.get("retention_days", "").strip()
    if not schedule or not cron_tz or not retention:
        return HealthCheck(
            label="Backup cron",
            value="Marker present but unparseable",
            severity="warn",
            detail="re-run ansible-playbook bootstrap.yml",
            data={"installed": True, "parsed": False},
        )
    try:
        retention_int = int(retention)
    except ValueError:
        return HealthCheck(
            label="Backup cron",
            value=f"retention_days is not an integer (got {retention!r})",
            severity="warn",
            detail="re-run ansible-playbook bootstrap.yml",
            data={"installed": True, "parsed": True, "retention_days": retention},
        )
    if retention_int <= 0:
        return HealthCheck(
            label="Backup cron",
            value=f"retention_days must be positive (got {retention_int})",
            severity="warn",
            detail="re-run ansible-playbook bootstrap.yml",
            data={
                "installed": True,
                "parsed": True,
                "retention_days": retention_int,
            },
        )
    value = (
        f"compadre-keys-backup at {schedule} "
        f"(CRON_TZ={cron_tz}, retention={retention_int}d)"
    )
    install_age = _marker_install_age_seconds(marker_path)
    return HealthCheck(
        label="Backup cron",
        value=value,
        severity="ok",
        detail=None,
        data={
            "installed": True,
            "schedule": schedule,
            "tz": cron_tz,
            "retention_days": retention_int,
            "install_age_seconds": install_age,
        },
    )


def _check_last_backup(
    backup_parent: Path,
    backup_pattern: str,
    *,
    allow_warn: bool,
    staleness_warn_seconds: int = 48 * 3600,
    backup_cron_install_age_seconds: int | None = None,
) -> HealthCheck:
    """Look for the most-recent backup artefact.

    With ``allow_warn=True`` (the Story 4.3 default) this returns ``warn``
    when no archive is present or the newest is older than
    ``staleness_warn_seconds``, and ``ok`` otherwise. The legacy
    ``allow_warn=False`` path keeps the Story 3.4 "forward-leaning info"
    shape and is retained as a rollback surface (via
    ``_BACKUP_NOT_IMPLEMENTED_INFO``) should backups ever need to be
    disabled mid-lifecycle.
    """
    matches: list[Path] = []
    try:
        if backup_parent.exists():
            matches = list(backup_parent.glob(backup_pattern))
    except OSError:
        matches = []

    if not allow_warn:
        if not matches:
            return HealthCheck(
                label="Last backup",
                value="never (Story 4.3 not yet shipped)",
                severity="info",
                detail=None,
                data={"available_after_story_4_3": False},
            )
        # A backup file somehow exists (manual or early adoption of 4.3).
        # Report the mtime but stay at info severity until the constant
        # flip enables warn/fail branches. Filter stat-races out so a
        # concurrent retention sweep cannot raise FileNotFoundError.
        legacy_survivors: list[tuple[Path, float]] = []
        for match in matches:
            try:
                legacy_survivors.append((match, match.stat().st_mtime))
            except OSError:
                continue
        if not legacy_survivors:
            return HealthCheck(
                label="Last backup",
                value="never (Story 4.3 not yet shipped)",
                severity="info",
                detail=None,
                data={"available_after_story_4_3": False},
            )
        newest, newest_mtime = max(legacy_survivors, key=lambda item: item[1])
        mtime_epoch = int(newest_mtime)
        mtime_iso = datetime.fromtimestamp(mtime_epoch, UTC).strftime(_ISO_UTC_FORMAT)
        age = int(
            (_utcnow() - datetime.fromtimestamp(mtime_epoch, UTC)).total_seconds()
        )
        return HealthCheck(
            label="Last backup",
            value=f"{mtime_iso} ({format_seconds_ago(age)})",
            severity="info",
            detail=None,
            data={
                "available_after_story_4_3": False,
                "path": str(newest),
                "mtime_iso": mtime_iso,
                "age_seconds": age,
            },
        )

    # Story 4.3 — warn / ok branches (active since the constant flipped).
    # Snapshot mtimes up-front so a concurrent retention sweep deleting an
    # archive between glob() and stat() can't raise FileNotFoundError out of
    # this check. Survivors-only, pick the newest.
    survivors: list[tuple[Path, float]] = []
    for match in matches:
        try:
            survivors.append((match, match.stat().st_mtime))
        except OSError:
            continue
    if not survivors:
        # Grace period mirrors _check_last_sweep: a freshly installed
        # backup cron that hasn't fired yet (deploy < 48h old) is the
        # legitimate post-bootstrap state, not operational drift. Only
        # flip to warn once the cron is past the staleness window — by
        # then it should have produced an archive.
        within_grace = (
            backup_cron_install_age_seconds is not None
            and backup_cron_install_age_seconds < staleness_warn_seconds
        )
        if within_grace:
            return HealthCheck(
                label="Last backup",
                value="never (backup cron freshly installed)",
                severity="info",
                detail="awaiting first 02:00 operator-TZ firing",
                data={
                    "available_after_story_4_3": True,
                    "path": None,
                    "backup_cron_install_age_seconds": (
                        backup_cron_install_age_seconds
                    ),
                },
            )
        return HealthCheck(
            label="Last backup",
            value="never",
            severity="warn",
            detail="run backup-keys.yml",
            data={"available_after_story_4_3": True, "path": None},
        )
    newest, newest_mtime = max(survivors, key=lambda item: item[1])
    mtime_epoch = int(newest_mtime)
    mtime_iso = datetime.fromtimestamp(mtime_epoch, UTC).strftime(_ISO_UTC_FORMAT)
    age = int((_utcnow() - datetime.fromtimestamp(mtime_epoch, UTC)).total_seconds())
    if age > staleness_warn_seconds:
        return HealthCheck(
            label="Last backup",
            value=f"{mtime_iso} ({format_seconds_ago(age)})",
            severity="warn",
            detail=f"expected within {staleness_warn_seconds // 3600}h",
            data={
                "available_after_story_4_3": True,
                "path": str(newest),
                "mtime_iso": mtime_iso,
                "age_seconds": age,
            },
        )
    return HealthCheck(
        label="Last backup",
        value=f"{mtime_iso} ({format_seconds_ago(age)})",
        severity="ok",
        detail=None,
        data={
            "available_after_story_4_3": True,
            "path": str(newest),
            "mtime_iso": mtime_iso,
            "age_seconds": age,
        },
    )


def _check_auth_cache(status: AggregatedCacheStatus | None) -> HealthCheck:
    """Wrap the existing ``AggregatedCacheStatus`` in a HealthCheck."""
    if status is None:
        return HealthCheck(
            label="Auth cache",
            value="no API replicas reporting (snapshot directory empty)",
            severity="info",
            detail=None,
            data={"auth_cache_available": False, "auth_cache": None},
        )
    hit_rate_pct = status.hit_rate * 100.0
    age_label = format_seconds_ago(status.snapshot_age_seconds)
    replica_word = "replica" if status.replicas == 1 else "replicas"
    value = (
        f"{status.replicas} {replica_word}, {status.entries} entries, "
        f"{hit_rate_pct:.1f}% hit rate, {age_label}"
    )
    return HealthCheck(
        label="Auth cache",
        value=value,
        severity="ok",
        detail=None,
        data={
            "auth_cache_available": True,
            "auth_cache": {
                "replicas": status.replicas,
                "entries": status.entries,
                "maxsize_per_replica": status.maxsize_per_replica,
                "hits": status.hits,
                "misses": status.misses,
                "hit_rate": status.hit_rate,
                "snapshot_age_seconds": status.snapshot_age_seconds,
                "process_uptime_seconds": status.process_uptime_seconds,
            },
        },
    )


def collect_health_report(
    *,
    conn: sqlite3.Connection,
    db_path: str,
    schema_version_expected: int,
    cron_marker_path: str,
    backup_parent: str,
    backup_pattern: str = "*.backup-*.tar.gz.enc",
    backup_cron_marker_path: str | None = None,
    sweep_staleness_warn_seconds: int = _SWEEP_STALENESS_WARN_SECONDS,
    runtime_billing_tz: str | None = None,
) -> HealthReport:
    """Build the full HealthReport.

    The caller owns the ``sqlite3.Connection`` so the CLI can map
    ``sqlite3.Error`` to exit 5 around ``get_connection`` /
    ``_apply_migrations`` / this call uniformly.

    Expected failure modes (missing DB file, empty snapshot dir, missing
    marker, missing backup) degrade to ``info`` / ``warn`` checks.
    Unexpected failures (``sqlite3.Error`` mid-query, ``OSError`` on a
    stat we shouldn't have reached) propagate — the CLI maps them to
    exit 5.
    """
    db_path_obj = Path(db_path)
    marker_path_obj = Path(cron_marker_path)
    backup_parent_obj = Path(backup_parent)
    if backup_cron_marker_path is None:
        # Same naming convention bootstrap.yml writes on the host: the
        # marker file sits alongside the DB with a ``.backup-cron-installed``
        # suffix. Operators who customise ``KEY_REGISTRY_DB_PATH`` re-run
        # bootstrap.yml, which writes the marker at the new derived path.
        backup_cron_marker_path = str(
            db_path_obj.with_name(db_path_obj.name + ".backup-cron-installed")
        )
    backup_cron_marker_obj = Path(backup_cron_marker_path)

    cron_check = _check_cron_marker(marker_path_obj)
    cron_installed = bool(cron_check.data and cron_check.data.get("installed"))
    cron_install_age_seconds = (cron_check.data or {}).get("install_age_seconds")
    if not isinstance(cron_install_age_seconds, int):
        cron_install_age_seconds = None
    backup_cron_check = _check_backup_cron_marker(backup_cron_marker_obj)
    backup_cron_install_age_seconds = (backup_cron_check.data or {}).get(
        "install_age_seconds"
    )
    if not isinstance(backup_cron_install_age_seconds, int):
        backup_cron_install_age_seconds = None

    snapshots = read_all_snapshots(db_path)
    cache_status = aggregate_cache_status(snapshots)

    # F57 — TZ drift cross-validation. Suppressed entirely when caller did
    # not pass ``runtime_billing_tz`` (legacy callers that haven't adopted
    # the new kwarg keep working unchanged).
    extra_checks: tuple[HealthCheck, ...] = ()
    if runtime_billing_tz is not None:
        extra_checks = (
            _check_billing_timezone_drift(
                cron_check,
                backup_cron_check,
                runtime_billing_tz=runtime_billing_tz,
            ),
        )

    checks: tuple[HealthCheck, ...] = (
        _check_db_path(db_path_obj),
        _check_schema_version(conn, expected=schema_version_expected),
        _check_customer_count(conn),
        _check_key_counts(conn),
        _check_last_sweep(
            conn,
            cron_installed=cron_installed,
            staleness_warn_seconds=sweep_staleness_warn_seconds,
            cron_install_age_seconds=cron_install_age_seconds,
        ),
        cron_check,
        backup_cron_check,
        *extra_checks,
        _check_last_backup(
            backup_parent_obj,
            backup_pattern,
            allow_warn=not _BACKUP_NOT_IMPLEMENTED_INFO,
            backup_cron_install_age_seconds=backup_cron_install_age_seconds,
        ),
        _check_auth_cache(cache_status),
    )

    is_healthy = all(check.severity in {"ok", "info"} for check in checks)
    return HealthReport(
        checks=checks,
        is_healthy=is_healthy,
        generated_at=_utcnow_iso(),
    )


def health_report_to_json(report: HealthReport) -> dict[str, object]:
    """Serialise a HealthReport to the canonical JSON shape."""
    warn_count = sum(1 for c in report.checks if c.severity == "warn")
    fail_count = sum(1 for c in report.checks if c.severity == "fail")
    return {
        "is_healthy": report.is_healthy,
        "generated_at": report.generated_at,
        "warn_count": warn_count,
        "fail_count": fail_count,
        "checks": [
            {
                "label": c.label,
                "severity": c.severity,
                "value": c.value,
                "detail": c.detail,
                "data": c.data,
            }
            for c in report.checks
        ],
    }
