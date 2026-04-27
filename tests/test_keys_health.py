"""Tests for ``compadre.keys.health`` — operational gate for ansible health.yml.

Lean by design (test-suite review 2026-04-25): each individual check
function had 3-5 unit tests; collapsed into integration-style tests that
exercise the full report so a regression in collector→check wiring is
caught.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from compadre.keys.audit import log_event
from compadre.keys.db import (
    _SCHEMA_VERSION,
    _apply_migrations,
    generate_customer_id,
    generate_key_id,
    get_connection,
)
from compadre.keys.health import (
    HealthCheck,
    HealthReport,
    collect_health_report,
    health_report_to_json,
)


def _seed_minimum(conn: sqlite3.Connection) -> None:
    cid = generate_customer_id()
    conn.execute(
        "INSERT INTO customer (id, name, email, tax_id_kind, tax_id_encrypted, "
        "notes, registered_at, deleted_at) VALUES "
        "(?, 'Helena', 'h@x.com', 'cpf', ?, '', '2026-04-10T10:00:00Z', NULL)",
        (cid, b"ciphertext"),
    )
    conn.execute(
        "INSERT INTO api_key (id, customer_id, key_hash, key_prefix, "
        "rate_limit_rpm, status, billing_mode, price_monthly, paid_until, "
        "created_at, last_used_at, notes) VALUES "
        "(?, ?, ?, 'AAAA1111', 60, 'active', 'manual', 149.0, NULL, "
        "'2026-04-10T12:00:00Z', NULL, '')",
        (generate_key_id(), cid, "a" * 64),
    )
    conn.commit()


def _collect(conn: sqlite3.Connection, db_path: Path) -> HealthReport:
    return collect_health_report(
        conn=conn,
        db_path=str(db_path),
        schema_version_expected=_SCHEMA_VERSION,
        cron_marker_path=str(db_path) + ".cron-installed",
        backup_parent=str(db_path.parent),
    )


def _check(report: HealthReport, label: str) -> HealthCheck:
    for c in report.checks:
        if c.label == label:
            return c
    raise AssertionError(f"No check labelled {label!r}")


def test_full_healthy_report_has_all_nine_checks_in_fixed_order(tmp_path: Path) -> None:
    """Story 3.4 + 4.4 contract: nine checks in fixed order, all ok/info,
    ``is_healthy=True`` only when every required artefact is in place."""
    db_path = tmp_path / "registry.db"
    conn = get_connection(db_path)
    try:
        _apply_migrations(conn)
        _seed_minimum(conn)
        with conn:
            log_event(
                conn,
                actor="cron:suspension-sweep",
                action="cron_suspend",
                target_type="system",
                target_id=None,
                details={"swept_count": 0, "cutoff_date": "2026-04-18"},
            )
        for kind, content in (
            ("cron", "schedule=30 0 * * *\ncron_tz=UTC"),
            ("backup-cron", "schedule=0 2 * * *\ncron_tz=UTC\nretention_days=14\n"),
        ):
            marker = Path(f"{db_path}.{kind}-installed")
            marker.write_text(content, encoding="utf-8")
            marker.chmod(0o600)
        # Fresh backup archive within 48h.
        (tmp_path / "registry.db.backup-2026-04-20T02-00-00Z.tar.gz.enc").write_bytes(
            b"encrypted-stub"
        )
        # Replica counter snapshot for Auth cache check.
        written = datetime.now(UTC) - timedelta(seconds=3)
        Path(f"{db_path}.counters.test-replica.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "written_at": written.strftime("%Y-%m-%dT%H:%M:%S.")
                    + f"{written.microsecond // 1000:03d}Z",
                    "process_uptime_seconds": 60,
                    "auth_cache": {
                        "entries": 42,
                        "maxsize": 4096,
                        "ttl_seconds": 60,
                        "hits": 1000,
                        "misses": 10,
                    },
                    "per_key": {},
                }
            )
        )
        report = _collect(conn, db_path)
    finally:
        conn.close()

    assert report.is_healthy is True
    assert [c.label for c in report.checks] == [
        "Database",
        "Schema version",
        "Customers",
        "Keys",
        "Last suspension sweep",
        "Cron",
        "Backup cron",
        "Last backup",
        "Auth cache",
    ]
    assert {c.severity for c in report.checks} <= {"ok", "info"}


def test_db_permissions_drift_emits_warn_and_flips_is_healthy(tmp_path: Path) -> None:
    """NFR13: registry.db mode 0600 is enforced. Drift surfaces loudly."""
    db_path = tmp_path / "registry.db"
    conn = get_connection(db_path)
    try:
        _apply_migrations(conn)
        db_path.chmod(0o644)
        report = _collect(conn, db_path)
    finally:
        conn.close()
    db_check = _check(report, "Database")
    assert db_check.severity == "warn"
    assert "0644" in db_check.value
    assert db_check.detail is not None and "0600" in db_check.detail
    assert report.is_healthy is False


def test_missing_or_stale_backup_and_cron_markers_warn(tmp_path: Path) -> None:
    """Story 4.3 + 4.4: ``Last backup`` must be < 48h; both ``Cron``
    (suspension) and ``Backup cron`` markers must exist. Either drops
    is_healthy."""
    db_path = tmp_path / "registry.db"

    # No artefacts at all → Last backup=never, both cron rows warn.
    conn = get_connection(db_path)
    try:
        _apply_migrations(conn)
        report = _collect(conn, db_path)
    finally:
        conn.close()
    assert _check(report, "Last backup").value == "never"
    assert _check(report, "Cron").severity == "warn"
    assert _check(report, "Backup cron").severity == "warn"
    assert report.is_healthy is False

    # Stale archive (> 48h old) → still warn with "expected within 48h" detail.
    archive = tmp_path / "registry.db.backup-2026-04-15T02-00-00Z.tar.gz.enc"
    archive.write_bytes(b"encrypted-stub")
    ancient = time.time() - (72 * 3600)
    os.utime(archive, (ancient, ancient))
    conn = get_connection(db_path)
    try:
        report = _collect(conn, db_path)
    finally:
        conn.close()
    backup = _check(report, "Last backup")
    assert backup.severity == "warn"
    assert "expected within 48h" in (backup.detail or "")


def test_freshly_installed_cron_with_no_history_is_info_not_warn(
    tmp_path: Path,
) -> None:
    """A freshly-installed suspension/backup cron that hasn't fired yet is
    the legitimate post-bootstrap state, not operational drift. Within the
    grace period (one staleness window from marker mtime) the freshness
    rows must report ``info`` so ``is_healthy`` stays True. Beyond the
    window, they escalate to ``warn``.

    Without this contract, ``ansible-playbook health.yml`` fails
    immediately after a green ``bootstrap.yml`` because the daily-suspension
    cron only fires at 00:30 operator-TZ and the backup cron at 02:00 — a
    deploy that ran at 22:00 has nothing to point at and would otherwise
    flip is_healthy=False.
    """
    db_path = tmp_path / "registry.db"

    # Write both markers with an mtime well inside the grace window.
    for kind, content in (
        ("cron", "schedule=30 0 * * *\ncron_tz=UTC"),
        ("backup-cron", "schedule=0 2 * * *\ncron_tz=UTC\nretention_days=14\n"),
    ):
        marker = Path(f"{db_path}.{kind}-installed")
        marker.write_text(content, encoding="utf-8")
        marker.chmod(0o600)
    # No audit row, no backup archive.
    conn = get_connection(db_path)
    try:
        _apply_migrations(conn)
        # Replica counter snapshot for Auth cache check.
        written = datetime.now(UTC) - timedelta(seconds=3)
        Path(f"{db_path}.counters.test-replica.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "written_at": written.strftime("%Y-%m-%dT%H:%M:%S.")
                    + f"{written.microsecond // 1000:03d}Z",
                    "process_uptime_seconds": 60,
                    "auth_cache": {
                        "entries": 0,
                        "maxsize": 4096,
                        "ttl_seconds": 60,
                        "hits": 0,
                        "misses": 0,
                    },
                    "per_key": {},
                }
            )
        )
        report = _collect(conn, db_path)
    finally:
        conn.close()

    sweep = _check(report, "Last suspension sweep")
    backup = _check(report, "Last backup")
    assert sweep.severity == "info"
    assert "freshly installed" in sweep.value
    assert backup.severity == "info"
    assert "freshly installed" in backup.value
    assert report.is_healthy is True

    # Push the markers back beyond the grace window → severity escalates.
    ancient = time.time() - (72 * 3600)
    for kind in ("cron", "backup-cron"):
        marker = Path(f"{db_path}.{kind}-installed")
        os.utime(marker, (ancient, ancient))
    conn = get_connection(db_path)
    try:
        report = _collect(conn, db_path)
    finally:
        conn.close()
    sweep = _check(report, "Last suspension sweep")
    backup = _check(report, "Last backup")
    assert sweep.severity == "warn"
    assert backup.severity == "warn"
    assert report.is_healthy is False


def test_full_healthy_report_has_ten_checks_when_runtime_billing_tz_passed(
    tmp_path: Path,
) -> None:
    """B9 — when the CLI passes ``runtime_billing_tz``, ``collect_health_report``
    inserts a 10th ``Timezone drift`` check between ``Backup cron`` and
    ``Last backup``. A refactor that drops or reorders the TZ probe must
    surface here, not silently in production. Companion to the 9-check
    legacy-caller test above.
    """
    db_path = tmp_path / "registry.db"
    conn = get_connection(db_path)
    try:
        _apply_migrations(conn)
        _seed_minimum(conn)
        with conn:
            log_event(
                conn,
                actor="cron:suspension-sweep",
                action="cron_suspend",
                target_type="system",
                target_id=None,
                details={"swept_count": 0, "cutoff_date": "2026-04-18"},
            )
        for kind, content in (
            ("cron", "schedule=30 0 * * *\ncron_tz=UTC"),
            ("backup-cron", "schedule=0 2 * * *\ncron_tz=UTC\nretention_days=14\n"),
        ):
            marker = Path(f"{db_path}.{kind}-installed")
            marker.write_text(content, encoding="utf-8")
            marker.chmod(0o600)
        (tmp_path / "registry.db.backup-2026-04-20T02-00-00Z.tar.gz.enc").write_bytes(
            b"encrypted-stub"
        )
        written = datetime.now(UTC) - timedelta(seconds=3)
        Path(f"{db_path}.counters.test-replica.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "written_at": written.strftime("%Y-%m-%dT%H:%M:%S.")
                    + f"{written.microsecond // 1000:03d}Z",
                    "process_uptime_seconds": 60,
                    "auth_cache": {
                        "entries": 42,
                        "maxsize": 4096,
                        "ttl_seconds": 60,
                        "hits": 1000,
                        "misses": 10,
                    },
                    "per_key": {},
                }
            )
        )
        report = collect_health_report(
            conn=conn,
            db_path=str(db_path),
            schema_version_expected=_SCHEMA_VERSION,
            cron_marker_path=str(db_path) + ".cron-installed",
            backup_parent=str(db_path.parent),
            runtime_billing_tz="UTC",
        )
    finally:
        conn.close()

    assert [c.label for c in report.checks] == [
        "Database",
        "Schema version",
        "Customers",
        "Keys",
        "Last suspension sweep",
        "Cron",
        "Backup cron",
        "Timezone drift",
        "Last backup",
        "Auth cache",
    ]
    assert report.is_healthy is True
    assert {c.severity for c in report.checks} <= {"ok", "info"}


def test_health_report_to_json_aggregates_warn_and_fail_counts() -> None:
    """JSON envelope is what monitoring consumes — must surface warn_count
    + fail_count + is_healthy honestly."""
    report = HealthReport(
        checks=(
            HealthCheck(label="A", value="ok", severity="ok"),
            HealthCheck(label="B", value="warn", severity="warn"),
            HealthCheck(label="C", value="fail", severity="fail"),
            HealthCheck(label="D", value="info", severity="info"),
        ),
        is_healthy=False,
        generated_at="2026-04-19T00:00:00Z",
    )
    payload = health_report_to_json(report)
    assert payload["is_healthy"] is False
    assert payload["warn_count"] == 1
    assert payload["fail_count"] == 1
    assert len(payload["checks"]) == 4
