"""Tests for the monetization-gated Phase 2a lifespan bootstrap.

The legacy lifespan is covered indirectly by every other test_api / test_health
case; this module only validates the branch Story 2.1 adds.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from fastapi import FastAPI

from compadre.config import get_settings
from compadre.main import lifespan


def _stub_engine_init(monkeypatch: pytest.MonkeyPatch) -> None:
    """Bypass heavy startup phases: WhisperEngine instantiation,
    ``ensure_whisper_model`` download, and engine warm-up."""
    import compadre.main as main_module

    class _StubEngine:
        def load_model(self, **_: object) -> None:
            pass

        def warm_up(self) -> None:
            pass

        @property
        def is_loaded(self) -> bool:
            return True

    monkeypatch.setattr(main_module, "WhisperEngine", _StubEngine)
    monkeypatch.setattr(main_module, "ensure_whisper_model", lambda **_: None)


@pytest.fixture()
def registry_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    """Set env vars so ``get_settings()`` builds a valid Settings."""
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("KEY_REGISTRY_DB_PATH", str(db_path))
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("HF_HOME", str(tmp_path / "models"))
    get_settings.cache_clear()
    return db_path


async def test_registry_bootstrap_migrates_caches_and_closes_db(
    monkeypatch: pytest.MonkeyPatch, registry_env: Path
) -> None:
    """Single integration cover: lifespan opens the registry, applies
    migrations, attaches the auth-cache, and closes the connection on
    shutdown."""
    _stub_engine_init(monkeypatch)

    opened: list[sqlite3.Connection] = []
    import compadre.main as main_module

    real_get_connection = main_module.get_connection

    def spy(path: Any) -> sqlite3.Connection:
        conn = real_get_connection(path)
        opened.append(conn)
        return conn

    monkeypatch.setattr(main_module, "get_connection", spy)

    app = FastAPI()
    cm = lifespan(app)
    await cm.__aenter__()
    try:
        # Auth cache wired with contractual sizing.
        snapshot = app.state.auth_cache.snapshot()
        assert snapshot["maxsize"] == 4096
        assert snapshot["ttl_seconds"] == 60
    finally:
        await cm.__aexit__(None, None, None)

    # Migrations applied — schema_version row matches the constant.
    from compadre.keys.db import _SCHEMA_VERSION

    conn = sqlite3.connect(registry_env)
    try:
        version_row = conn.execute("SELECT MAX(version) FROM schema_version").fetchone()
        assert version_row[0] == _SCHEMA_VERSION
    finally:
        conn.close()

    # Connection closed on shutdown — using it raises ProgrammingError.
    assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError):
        opened[0].execute("SELECT 1").fetchone()
