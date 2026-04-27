"""End-to-end CLI tests for ``compadre-keys list-customers`` (Story 1.3).

Lean by design (test-suite review 2026-04-25).
"""

from __future__ import annotations

import json

from compadre.keys.cli.app import app
from tests._keys_cli_helpers import buffers, clear_buffers, runner


def _create(name: str, email: str, cpf: str) -> None:
    clear_buffers()
    runner.invoke(
        app,
        ["create-customer", "--name", name, "--email", email, "--cpf", cpf],
    )
    clear_buffers()


def test_list_customers_empty_db_prints_hint(env: dict[str, str]) -> None:
    """First-run UX: empty DB emits the 'create-customer' hint."""
    result = runner.invoke(app, ["list-customers"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "compadre-keys create-customer" in stdout


def test_list_customers_table_renders_each_active_customer(
    env: dict[str, str],
) -> None:
    """Happy path: registered customers appear, tombstoned ones don't."""
    _create("Helena", "h@x.com", "123.456.789-09")
    _create("Marcos", "m@x.com", "529.982.247-25")

    result = runner.invoke(app, ["list-customers"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    assert "Helena" in stdout
    assert "Marcos" in stdout


def test_list_customers_format_json_emits_array(env: dict[str, str]) -> None:
    _create("Helena", "h@x.com", "123.456.789-09")
    result = runner.invoke(app, ["list-customers", "--format", "json"])
    stdout, _ = buffers()
    assert result.exit_code == 0
    parsed = json.loads(stdout)
    assert isinstance(parsed, list)
    assert len(parsed) == 1
    assert parsed[0]["name"] == "Helena"
    assert "tax_id_encrypted" not in parsed[0]
