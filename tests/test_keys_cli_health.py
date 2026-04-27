"""End-to-end CLI tests for ``compadre-keys health`` (Story 3.4).

Lean by design (test-suite review 2026-04-25): one table-mode happy
path (exit 0 always), one JSON unhealthy path (exit 7 contract).
"""

from __future__ import annotations

import json

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import buffers, runner


def test_health_table_mode_always_exits_zero(env: dict[str, str]) -> None:
    """Read-tier UX rule: table-mode invocations exit 0 even when unhealthy
    (operator scrolls visual output; signal lives in the panel)."""
    result = runner.invoke(app, ["health"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert len(stdout) > 0


def test_health_json_mode_unhealthy_exits_7(env: dict[str, str]) -> None:
    """JSON mode is the monitor-friendly entrypoint: exits 7 on any
    warn/fail check (a fresh DB with no backup or cron markers is
    unhealthy by definition)."""
    result = runner.invoke(app, ["health", "--format", "json"])
    stdout, _ = buffers()
    assert result.exit_code == 7
    parsed = json.loads(stdout)
    assert parsed["is_healthy"] is False
    assert "checks" in parsed
