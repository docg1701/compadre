"""Shared pytest configuration and marker registration."""

from __future__ import annotations

import asyncio
import sqlite3
import time
from collections.abc import AsyncIterator, Callable, Iterator
from datetime import date
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr
from rich.console import Console

from compadre.config import Settings, get_registry_settings, get_settings
from compadre.keys.cache import AuthCache
from compadre.keys.cli import _clock as _cli_clock
from compadre.keys.cli import output as _keys_cli_output
from compadre.keys.db import _apply_migrations, get_connection
from compadre.main import create_app
from compadre.rate_limit import RateLimiter


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers", "slow: marks tests requiring real model inference"
    )
    config.addinivalue_line(
        "markers",
        "non_regression: CI non-regression suite (PRD Non-Regression Contract;"
        " some tests spawn docker, others use ASGI TestClient)",
    )


@pytest.fixture(autouse=True)
def _clear_settings_cache() -> Iterator[None]:
    """F33/F41: clear LRU caches before AND after each test so a fixture
    cannot leak across tests via process state."""
    get_settings.cache_clear()
    get_registry_settings.cache_clear()
    yield
    get_settings.cache_clear()
    get_registry_settings.cache_clear()


@pytest.fixture(autouse=True)
def _reset_cli_consoles_to_buffers() -> Iterator[tuple[StringIO, StringIO]]:
    """F41: every test gets fresh in-memory Consoles in place of the CLI
    output module's stdout/stderr singletons — no terminal pollution, no
    cross-test bleed."""
    stdout_buf = StringIO()
    stderr_buf = StringIO()
    _keys_cli_output._reset_for_tests()
    _keys_cli_output._stdout_console = Console(
        file=stdout_buf, force_terminal=False, no_color=True, width=120
    )
    _keys_cli_output._stderr_console = Console(
        file=stderr_buf, force_terminal=False, no_color=True, width=120
    )
    try:
        yield stdout_buf, stderr_buf
    finally:
        _keys_cli_output._reset_for_tests()


@pytest.fixture()
def fernet_key() -> str:
    return Fernet.generate_key().decode()


@pytest.fixture()
def mock_settings(fernet_key: str) -> Settings:
    return Settings(key_registry_encryption_key=SecretStr(fernet_key))


@pytest.fixture()
def mock_engine() -> MagicMock:
    """Mock WhisperEngine with the canonical STT shape."""
    engine = MagicMock()
    engine.is_loaded = True
    engine.transcribe.return_value = (
        [],  # segments
        MagicMock(language="pt", language_probability=0.95, duration=0.0),
    )
    return engine


@pytest.fixture()
def registry_db_path(tmp_path: Path) -> Path:
    return tmp_path / "registry.db"


@pytest.fixture()
def registry_conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    """Fresh registry connection with migrations applied; saves
    ``get_connection + _apply_migrations + try/finally close`` boilerplate
    across db/auth tests."""
    conn = get_connection(tmp_path / "registry.db")
    try:
        _apply_migrations(conn)
        yield conn
    finally:
        conn.close()


@pytest.fixture()
def app(
    mock_engine: MagicMock,
    monkeypatch: pytest.MonkeyPatch,
    registry_db_path: Path,
    fernet_key: str,
) -> Any:
    """Registry-mode FastAPI app with mocked engine state. Integration
    tests seed customer/api_key rows via ``app.state.registry_db`` directly."""
    monkeypatch.setenv("KEY_REGISTRY_DB_PATH", str(registry_db_path))
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", fernet_key)
    monkeypatch.setenv(
        "OPERATOR_CONTACT_HINT", "Contact Ana on WhatsApp: +55 11 90000-0000"
    )
    get_settings.cache_clear()

    application = create_app()
    application.state.settings = get_settings()
    application.state.engine = mock_engine
    application.state.ready = True
    application.state.queue_lock = asyncio.Lock()
    application.state.queue_depth = 0
    application.state.start_time = time.monotonic()

    conn = get_connection(registry_db_path)
    _apply_migrations(conn)
    application.state.registry_db = conn
    application.state.auth_cache = AuthCache()
    application.state.rate_limiter = RateLimiter.build()

    try:
        yield application
    finally:
        conn.close()


@pytest.fixture()
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture()
def registry_db(app: Any) -> sqlite3.Connection:
    conn: sqlite3.Connection = app.state.registry_db
    return conn


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """F41: per-CLI-test env (tmp registry path + Fernet key); promoted
    from local fixtures duplicated across 19 CLI test files."""
    key = Fernet.generate_key().decode()
    db_path = tmp_path / "registry.db"
    monkeypatch.setenv("KEY_REGISTRY_DB_PATH", str(db_path))
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", key)
    return {"db_path": str(db_path), "key": key}


@pytest.fixture()
def freeze_today(monkeypatch: pytest.MonkeyPatch) -> Callable[[date], None]:
    """Pin ``_clock.resolve_today_in_tz`` to a fixed date for CLI tests."""

    def _factory(frozen: date) -> None:
        monkeypatch.setattr(_cli_clock, "resolve_today_in_tz", lambda _tz: frozen)

    return _factory


@pytest.fixture()
def auth_headers(app: Any, monkeypatch: pytest.MonkeyPatch) -> dict[str, str]:
    """Bearer-auth header dict with a generous-rpm seeded key so tests can
    fire dozens of requests without tripping the per-key limit."""
    from compadre.keys import auth as auth_module
    from tests._keys_fixtures import seed_customer_and_key

    monkeypatch.setattr(
        auth_module, "_today_in_billing_tz", lambda _s: date(2026, 4, 17)
    )
    plaintext, _kid, _cid = seed_customer_and_key(
        app.state.registry_db, rate_limit_rpm=9999
    )
    return {"Authorization": f"Bearer {plaintext}"}


# ---------------------------------------------------------------------------
# Slow (real-model) test fixtures
# ---------------------------------------------------------------------------

_REFERENCE_AUDIO_NAME = "audio-de-teste-01.ogg"


@pytest.fixture(scope="session")
def reference_audio_path() -> Path:
    """Path to the reference test audio file at the repo root.

    ``audio-de-teste-01.ogg`` — ~14s monologue Portuguese, 16kHz mono OPUS
    in OGG container. Used as the canonical input for real-model integration
    tests.
    """
    repo_root = Path(__file__).resolve().parent.parent
    path = repo_root / _REFERENCE_AUDIO_NAME
    if not path.is_file():
        pytest.fail(
            f"Reference audio file not found at {path}. "
            f"Run `git lfs pull` or copy a test audio file to {path}."
        )
    return path


@pytest.fixture(scope="session")
def real_engine() -> Any:
    """Load the real faster-whisper model once per test session.

    The model is ~1.5 GB and takes 5-15 seconds to load on first call
    (longer if HuggingFace download is needed). Session scope ensures it
    loads only once across all ``@pytest.mark.slow`` tests.
    """
    from compadre.stt import WhisperEngine

    engine = WhisperEngine()
    engine.load_model()
    return engine


@pytest.fixture(scope="session")
def multi_format_audio_dir(
    reference_audio_path: Path, tmp_path_factory: pytest.TempPathFactory
) -> Path:
    """Generate test audio files in all 5 supported formats from the
    reference OGG file, stored in a session-scoped temp directory.

    Returns the directory path containing:
        audio.ogg   — original OPUS-in-OGG (copy)
        audio.mp3   — MP3
        audio.m4a   — M4A (AAC)
        audio.wav   — 16kHz mono PCM WAV
        audio.webm  — OPUS-in-WebM
    """
    import shutil
    import subprocess

    out_dir = tmp_path_factory.mktemp("audio-formats")

    # Copy original OGG
    ogg_path = out_dir / "audio.ogg"
    shutil.copy2(reference_audio_path, ogg_path)

    formats: list[tuple[str, str, list[str]]] = [
        ("audio.mp3", "mp3", ["-b:a", "64k"]),
        ("audio.m4a", "m4a", ["-c:a", "aac", "-b:a", "64k"]),
        ("audio.wav", "wav", ["-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1"]),
        ("audio.webm", "webm", ["-c:a", "libopus", "-b:a", "32k"]),
    ]

    for out_name, _fmt, extra_args in formats:
        out_path = out_dir / out_name
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            str(reference_audio_path),
            "-vn",
            *extra_args,
            str(out_path),
        ]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        except subprocess.CalledProcessError as exc:
            pytest.fail(
                f"Failed to convert reference audio to {out_name}: "
                f"{exc.stderr.strip()}"
            )
    return out_dir
