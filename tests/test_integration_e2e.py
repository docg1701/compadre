"""End-to-end HTTP integration tests: full round-trip through the FastAPI
app with the **real** faster-whisper engine loaded once per session.

Covers:
- All 5 ``response_format`` values via HTTP
- Language, model, temperature form fields
- Auth scenarios: valid, missing, wrong shape, suspended, revoked, expired
- Basic upload boundaries

All tests require ``@pytest.mark.slow`` (real model loaded once per session).
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from compadre.config import get_settings
from compadre.keys.cache import AuthCache
from compadre.keys.db import _apply_migrations, get_connection
from compadre.main import create_app
from compadre.rate_limit import RateLimiter
from tests._keys_fixtures import seed_customer_and_key

pytestmark = pytest.mark.slow


# ── Fixtures ──────────────────────────────────────────────────────────


@pytest.fixture()
def e2e_app(
    real_engine: Any,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> Any:
    """FastAPI app with the real Whisper engine injected (bypasses lifespan)."""
    fernet_key = Fernet.generate_key().decode()
    registry_db_path = tmp_path / "e2e_registry.db"

    monkeypatch.setenv("KEY_REGISTRY_DB_PATH", str(registry_db_path))
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", fernet_key)
    monkeypatch.setenv(
        "OPERATOR_CONTACT_HINT", "Contact Ana on WhatsApp: +55 11 90000-0000"
    )
    get_settings.cache_clear()

    application = create_app()
    application.state.settings = get_settings()
    application.state.engine = real_engine
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
async def e2e_client(e2e_app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(
        transport=ASGITransport(app=e2e_app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture()
def e2e_auth_headers(
    monkeypatch: pytest.MonkeyPatch, e2e_app: Any
) -> dict[str, str]:
    """Bearer-auth header with a high-RPM seeded key."""
    from datetime import date

    from compadre.keys import auth as auth_module

    monkeypatch.setattr(
        auth_module, "_today_in_billing_tz", lambda _s: date(2026, 4, 17)
    )
    plaintext, _kid, _cid = seed_customer_and_key(
        e2e_app.state.registry_db, rate_limit_rpm=9999
    )
    return {"Authorization": f"Bearer {plaintext}"}


# ── Response formats via HTTP ─────────────────────────────────────────


@pytest.mark.parametrize(
    "fmt, expected_ct_prefix",
    [
        ("text", "text/plain"),
        ("json", "application/json"),
        ("verbose_json", "application/json"),
        ("srt", "text/plain"),
        ("vtt", "text/vtt"),
    ],
)
async def test_e2e_response_format_via_http(
    e2e_client: AsyncClient,
    e2e_auth_headers: dict[str, str],
    reference_audio_path: Path,
    fmt: str,
    expected_ct_prefix: str,
) -> None:
    """Each response_format returns 200 with correct Content-Type."""
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            data={"response_format": fmt},
            headers=e2e_auth_headers,
        )
    assert response.status_code == 200, (
        f"{fmt}: expected 200, got {response.status_code}: {response.text[:300]}"
    )
    assert response.headers["content-type"].startswith(expected_ct_prefix), (
        f"{fmt}: expected Content-Type starting with {expected_ct_prefix!r}, "
        f"got {response.headers.get('content-type')!r}"
    )

    # Structural checks per format
    if fmt == "text":
        assert isinstance(response.text, str) and len(response.text.strip()) > 0
    elif fmt == "json":
        body = response.json()
        assert "text" in body
    elif fmt == "verbose_json":
        body = response.json()
        for key in ("text", "language", "duration", "segments"):
            assert key in body, f"verbose_json missing key: {key}"
    elif fmt == "srt":
        assert isinstance(response.text, str)
        assert "-->" in response.text
    elif fmt == "vtt":
        assert response.text.startswith("WEBVTT")


# ── Form fields ───────────────────────────────────────────────────────


async def test_e2e_language_form_field(
    e2e_client: AsyncClient,
    e2e_auth_headers: dict[str, str],
    reference_audio_path: Path,
) -> None:
    """``language=pt`` form field is respected."""
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            data={"language": "pt", "response_format": "verbose_json"},
            headers=e2e_auth_headers,
        )
    assert response.status_code == 200
    body = response.json()
    assert body["language"].lower() in ("pt", "portuguese"), (
        f"Expected pt language, got: {body['language']!r}"
    )


async def test_e2e_model_form_field_accepted(
    e2e_client: AsyncClient,
    e2e_auth_headers: dict[str, str],
    reference_audio_path: Path,
) -> None:
    """``model`` form field is accepted (ignored in MVP)."""
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            data={"model": "whisper-1"},
            headers=e2e_auth_headers,
        )
    assert response.status_code == 200


async def test_e2e_temperature_form_field_accepted(
    e2e_client: AsyncClient,
    e2e_auth_headers: dict[str, str],
    reference_audio_path: Path,
) -> None:
    """``temperature`` form field is accepted (ignored in MVP)."""
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            data={"temperature": "0.5"},
            headers=e2e_auth_headers,
        )
    assert response.status_code == 200


# ── Auth scenarios ────────────────────────────────────────────────────


async def test_e2e_missing_auth_header_returns_401(
    e2e_client: AsyncClient, reference_audio_path: Path
) -> None:
    """Missing Authorization header → 401 authentication_error."""
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
        )
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["type"] == "authentication_error"


async def test_e2e_bearer_empty_token_returns_401(
    e2e_client: AsyncClient, reference_audio_path: Path
) -> None:
    """``Bearer`` with empty token → 401."""
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            headers={"Authorization": "Bearer "},
        )
    assert response.status_code == 401


async def test_e2e_bearer_wrong_shape_returns_401(
    e2e_client: AsyncClient, reference_audio_path: Path
) -> None:
    """Token not starting with ``cmpd_live_`` → 401, enumeration-resistant."""
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            headers={"Authorization": "Bearer wrong-shape-key"},
        )
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["type"] == "authentication_error"
    # Enumeration resistance: message must not reveal whether key existed
    msg_lower = body["error"]["message"].lower()
    assert "invalid" in msg_lower or "missing" in msg_lower


async def test_e2e_basic_scheme_returns_401(
    e2e_client: AsyncClient, reference_audio_path: Path
) -> None:
    """``Basic`` scheme → 401."""
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            headers={"Authorization": "Basic dXNlcjpwYXNz"},
        )
    assert response.status_code == 401


async def test_e2e_suspended_key_returns_401(
    e2e_app: Any,
    e2e_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    reference_audio_path: Path,
) -> None:
    """Suspended key → 401 with hint."""
    from datetime import date

    from compadre.keys import auth as auth_module

    monkeypatch.setattr(
        auth_module, "_today_in_billing_tz", lambda _s: date(2026, 4, 17)
    )
    pt, _, _ = seed_customer_and_key(
        e2e_app.state.registry_db,
        customer_email="suspended@x.com",
        status="suspended",
    )
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert response.status_code == 401
    body = response.json()
    assert "suspended" in body["error"]["message"].lower()
    assert "Contact Ana" in body["error"]["hint"]


async def test_e2e_revoked_key_returns_401(
    e2e_app: Any,
    e2e_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    reference_audio_path: Path,
) -> None:
    """Revoked key → 401 with hint."""
    from datetime import date

    from compadre.keys import auth as auth_module

    monkeypatch.setattr(
        auth_module, "_today_in_billing_tz", lambda _s: date(2026, 4, 17)
    )
    pt, _, _ = seed_customer_and_key(
        e2e_app.state.registry_db,
        customer_email="revoked@x.com",
        status="revoked",
    )
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert response.status_code == 401
    body = response.json()
    assert "revoked" in body["error"]["message"].lower()
    assert "Contact Ana" in body["error"]["hint"]


async def test_e2e_expired_key_returns_401(
    e2e_app: Any,
    e2e_client: AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    reference_audio_path: Path,
) -> None:
    """Expired manual key (past paid_until) → 401."""
    from datetime import date

    from compadre.keys import auth as auth_module

    monkeypatch.setattr(
        auth_module, "_today_in_billing_tz", lambda _s: date(2026, 4, 17)
    )
    pt, _, _ = seed_customer_and_key(
        e2e_app.state.registry_db,
        customer_email="expired@x.com",
        paid_until="2020-01-01",
    )
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            headers={"Authorization": f"Bearer {pt}"},
        )
    assert response.status_code == 401
    body = response.json()
    assert "suspended" in body["error"]["message"].lower()


async def test_e2e_valid_key_returns_200(
    e2e_client: AsyncClient,
    e2e_auth_headers: dict[str, str],
    reference_audio_path: Path,
) -> None:
    """Valid key → 200, transcription returned."""
    with reference_audio_path.open("rb") as fh:
        response = await e2e_client.post(
            "/v1/audio/transcriptions",
            files={"file": (reference_audio_path.name, fh, "audio/ogg")},
            headers=e2e_auth_headers,
        )
    assert response.status_code == 200
    assert len(response.text.strip()) > 0
