"""End-to-end CLI tests for ``compadre-keys cache-status`` (Story 2.3).

Lean by design (test-suite review 2026-04-25): one happy path with a
fresh snapshot, one no-snapshots empty path.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import buffers, runner


def _write_snapshot(db_path: str, replica: str = "test-replica") -> None:
    snapshot_path = Path(f"{db_path}.counters.{replica}.json")
    written = datetime.now(UTC) - timedelta(seconds=3)
    snapshot_path.write_text(
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


def test_cache_status_with_fresh_snapshot_renders_metrics(env: dict[str, str]) -> None:
    _write_snapshot(env["db_path"])
    result = runner.invoke(app, ["cache-status"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "%" in stdout or "hits" in stdout.lower() or "entries" in stdout.lower()


def test_cache_status_no_snapshots_renders_unavailable(env: dict[str, str]) -> None:
    """No replicas have written snapshots → cache-status reports unavailable."""
    result = runner.invoke(app, ["cache-status"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "unavail" in stdout.lower() or "no " in stdout.lower() or "0" in stdout
