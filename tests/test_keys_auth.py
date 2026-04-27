"""Tests for ``compadre.keys.auth.verify_api_key_registry`` (SECURITY-CRITICAL).

The auth path on every ``POST /v1/audio/speech``. Each test defends a
security property no integration test reliably catches: F18 cache-hit
equalisation (timing-attack), DB-miss dummy compare (enumeration),
status branches, fail-closed on DB error, no-PII-in-logs, non-ASCII safe.
"""

from __future__ import annotations

import logging
import sqlite3
from datetime import date
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr

from compadre.config import RegistrySettings
from compadre.exceptions import (
    ApiKeyNotFoundError,
    ApiKeyRevokedError,
    ApiKeySuspendedError,
    KeyRegistryError,
)
from compadre.keys import auth as auth_module
from compadre.keys.auth import verify_api_key_registry
from compadre.keys.cache import AuthCache
from compadre.keys.tokens import hash_api_key
from tests._keys_fixtures import seed_customer_and_key

CONTACT_HINT = "Contact Ana on WhatsApp: +55 11 90000-0000"


def _make_request(conn: sqlite3.Connection, cache: AuthCache) -> Any:
    request = MagicMock()
    request.app.state.settings = RegistrySettings(
        operator_contact_hint=CONTACT_HINT,
        key_registry_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    request.app.state.auth_cache = cache
    request.app.state.registry_db = conn
    request.state = MagicMock()
    return request


class _CapturingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture()
def auth_log_handler() -> Any:
    handler = _CapturingHandler()
    logger = logging.getLogger("keys.auth")
    logger.addHandler(handler)
    try:
        yield handler
    finally:
        logger.removeHandler(handler)


async def test_cache_miss_db_hit_caches_payload_and_sets_request_state(
    registry_conn: sqlite3.Connection,
) -> None:
    """First lookup: DB miss → DB hit → payload cached → request.state set."""
    plaintext, kid, _ = seed_customer_and_key(registry_conn)
    cache = AuthCache()
    request = _make_request(registry_conn, cache)

    await verify_api_key_registry(request, token=plaintext)

    assert cache.snapshot()["entries"] == 1
    cached = cache.get(hash_api_key(plaintext))
    assert cached is not None and cached.key_id == kid
    assert request.state.auth_payload == cached


async def test_cache_hit_runs_equalisation_select_against_dummy(
    registry_conn: sqlite3.Connection,
) -> None:
    """F18: cache hit MUST run the same indexed SELECT as a miss (against a
    never-matching dummy) so timing distinguishability is removed. A
    cache-residency oracle for active keys would be a known-key probe."""
    plaintext, _, _ = seed_customer_and_key(registry_conn)
    cache = AuthCache()
    request = _make_request(registry_conn, cache)
    await verify_api_key_registry(request, token=plaintext)  # populate

    statements: list[str] = []
    registry_conn.set_trace_callback(statements.append)
    try:
        await verify_api_key_registry(request, token=plaintext)
    finally:
        registry_conn.set_trace_callback(None)

    select_stmts = [s for s in statements if s.upper().lstrip().startswith("SELECT")]
    assert len(select_stmts) == 1, statements


async def test_db_miss_runs_dummy_compare_and_does_not_cache(
    registry_conn: sqlite3.Connection, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Unknown token: ApiKeyNotFoundError + dummy compare runs (anti-enum) +
    cache stays empty (a miss MUST NOT pollute the cache)."""
    seed_customer_and_key(registry_conn)
    cache = AuthCache()

    calls: list[tuple[bytes, bytes]] = []
    real_compare = auth_module.hmac.compare_digest

    def spy(a: Any, b: Any) -> bool:
        calls.append((a, b))
        return bool(real_compare(a, b))

    monkeypatch.setattr(auth_module.hmac, "compare_digest", spy)

    request = _make_request(registry_conn, cache)
    with pytest.raises(ApiKeyNotFoundError):
        await verify_api_key_registry(request, token="unknown-xyz")

    assert len(calls) == 1
    assert cache.snapshot()["entries"] == 0


async def test_status_and_billing_branches_raise_correct_errors(
    tmp_path: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Suspended → 401 suspended; revoked → 401 revoked; expired manual →
    401 suspended; complimentary always allowed."""
    from compadre.keys.db import _apply_migrations, get_connection

    monkeypatch.setattr(
        auth_module, "_today_in_billing_tz", lambda _s: date(2026, 4, 17)
    )

    cases: list[tuple[dict[str, Any], type[Exception] | None]] = [
        ({"status": "suspended"}, ApiKeySuspendedError),
        ({"status": "revoked"}, ApiKeyRevokedError),
        ({"paid_until": "2020-01-01"}, ApiKeySuspendedError),
        ({"billing_mode": "complimentary", "paid_until": None}, None),
        # B10 regression: a freshly-issued manual key (paid_until=None) is in
        # the trial window — auth must allow it. Operator transitions out of
        # trial via ``mark-paid``; sweep won't suspend NULL rows.
        ({"billing_mode": "manual", "paid_until": None}, None),
    ]
    for kwargs, expected_exc in cases:
        label = expected_exc.__name__ if expected_exc else "comp"
        conn = get_connection(tmp_path / f"r-{label}.db")
        try:
            _apply_migrations(conn)
            plaintext, _, _ = seed_customer_and_key(conn, **kwargs)
            request = _make_request(conn, AuthCache())
            if expected_exc:
                with pytest.raises(expected_exc):
                    await verify_api_key_registry(request, token=plaintext)
            else:
                await verify_api_key_registry(request, token=plaintext)
                assert request.state.auth_payload is not None
        finally:
            conn.close()


async def test_db_error_fails_closed_with_key_registry_error(
    registry_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    auth_log_handler: _CapturingHandler,
) -> None:
    """A SQLite error must NOT leak as 200 — fail closed via KeyRegistryError.
    The error log line is the operator's only signal that the registry is sick."""
    request = _make_request(registry_conn, AuthCache())

    def boom(*_a: object, **_kw: object) -> object:
        raise sqlite3.OperationalError("database is closed")

    monkeypatch.setattr(auth_module, "lookup_key_auth", boom)

    with pytest.raises(KeyRegistryError):
        await verify_api_key_registry(request, token="any-token")

    error_records = [r for r in auth_log_handler.records if r.levelno == logging.ERROR]
    assert error_records and "unreachable or corrupt" in error_records[0].getMessage()


async def test_log_records_carry_no_plaintext_hash_or_customer_name(
    registry_conn: sqlite3.Connection,
    monkeypatch: pytest.MonkeyPatch,
    auth_log_handler: _CapturingHandler,
) -> None:
    """LGPD + token discipline: log lines NEVER carry secret-shaped values."""
    plaintext, _, _ = seed_customer_and_key(
        registry_conn, paid_until="not-a-date", customer_name="Helena Ribeiro"
    )
    request = _make_request(registry_conn, AuthCache())
    monkeypatch.setattr(
        auth_module, "_today_in_billing_tz", lambda _s: date(2026, 4, 17)
    )

    with pytest.raises(ApiKeySuspendedError):
        await verify_api_key_registry(request, token=plaintext)

    for record in auth_log_handler.records:
        body = record.getMessage()
        assert "cmpd_live_" not in body
        assert hash_api_key(plaintext) not in body
        assert "Helena Ribeiro" not in body


async def test_non_ascii_bearer_token_returns_not_found_not_500(
    registry_conn: sqlite3.Connection,
) -> None:
    """Non-ASCII tokens must not bubble UnicodeEncodeError as a 500."""
    cache = AuthCache()
    request = _make_request(registry_conn, cache)
    with pytest.raises(ApiKeyNotFoundError):
        await verify_api_key_registry(request, token="café_não_existe")
    assert cache.snapshot()["entries"] == 0
