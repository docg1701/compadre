"""Tests for ``compadre.config`` — only custom validators / business rules.

Lean by design (test-suite review 2026-04-25): per-field default + env-var
read tests collapsed away — those are pydantic-settings framework
guarantees. What survives are the custom validator bodies (Fernet,
IANA timezone, backup passphrase length) plus the LRU cache.
"""

from __future__ import annotations

import pytest
from pydantic import SecretStr, ValidationError

from compadre.config import Settings, get_settings


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> None:
    get_settings.cache_clear()


def test_settings_loads_env_vars_and_defaults(
    monkeypatch: pytest.MonkeyPatch, fernet_key: str
) -> None:
    """One smoke confirming env-var pickup + defaults wire through."""
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", fernet_key)
    monkeypatch.setenv("PORT", "9000")

    s = Settings()
    assert s.port == 9000
    # Defaults wire through.
    assert s.whisper_model_size == "large-v3-turbo"
    assert s.whisper_cpu_threads == 4
    assert s.billing_timezone == "UTC"


def test_missing_encryption_key_raises_validation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """KEY_REGISTRY_ENCRYPTION_KEY is required-no-default."""
    monkeypatch.delenv("KEY_REGISTRY_ENCRYPTION_KEY", raising=False)
    with pytest.raises(ValidationError):
        Settings()


def test_malformed_fernet_key_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """The validator rejects bad Fernet shapes."""
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", "not-a-fernet-key")
    with pytest.raises(ValidationError) as exc_info:
        Settings()
    assert "Fernet" in str(exc_info.value)


def test_invalid_iana_timezone_rejected(
    monkeypatch: pytest.MonkeyPatch, fernet_key: str
) -> None:
    """``BILLING_TIMEZONE`` must be IANA — typos like ``BRT`` or
    ``America/Sao-Paulo`` (hyphen) fail closed at boot."""
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", fernet_key)
    monkeypatch.setenv("BILLING_TIMEZONE", "America/Sao-Paulo")
    with pytest.raises(ValidationError):
        Settings()


def test_short_backup_passphrase_rejected_empty_accepted(
    monkeypatch: pytest.MonkeyPatch, fernet_key: str
) -> None:
    """Empty passphrase accepted at boot (deferred-bootstrap OK);
    < 16 chars rejected."""
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", fernet_key)
    monkeypatch.setenv("BACKUP_PASSPHRASE", "")
    s = Settings()
    assert s.backup_passphrase == SecretStr("")  # empty allowed

    monkeypatch.setenv("BACKUP_PASSPHRASE", "too-short")
    with pytest.raises(ValidationError):
        Settings()


def test_get_settings_is_lru_cached_returns_same_instance(
    monkeypatch: pytest.MonkeyPatch, fernet_key: str
) -> None:
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", fernet_key)
    a = get_settings()
    b = get_settings()
    assert a is b
