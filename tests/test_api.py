"""HTTP-boundary tests for ``/v1/audio/transcriptions`` (and ``/health`` 405).

Lean by design (test-suite review 2026-04-25): the contract is the
status code + JSON envelope shape + audit invariants. Rate-limit +
counters + snapshot-lifespan tests live in ``test_counters.py``.
"""

from __future__ import annotations

import io
import sqlite3
import wave
from datetime import date
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient

from tests._keys_fixtures import seed_customer_and_key


def _make_wav_bytes(duration_ms: int = 1000) -> bytes:
    """Generate a minimal mono WAV for upload tests."""
    sample_rate = 16000
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    pcm = b"\x00\x00" * num_samples
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


@pytest.fixture(autouse=True)
def _freeze_clock(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin auth's ``today`` so ``paid_until=2099-12-31`` seed rows stay active."""
    from compadre.keys import auth as auth_module

    monkeypatch.setattr(
        auth_module, "_today_in_billing_tz", lambda _s: date(2026, 4, 17)
    )


async def test_transcription_happy_path_all_formats(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """All five response formats return correct content-type and body."""
    wav_bytes = _make_wav_bytes()
    for fmt, ctype_prefix, check in (
        ("text", "text/plain", lambda r: isinstance(r.text, str)),
        ("json", "application/json", lambda r: "text" in r.json()),
        (
            "verbose_json",
            "application/json",
            lambda r: "segments" in r.json() and "language" in r.json(),
        ),
        ("srt", "text/plain", lambda r: isinstance(r.text, str)),
        ("vtt", "text/plain", lambda r: r.text.startswith("WEBVTT") or r.text == ""),
    ):
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
            data={"response_format": fmt},
            headers=auth_headers,
        )
        assert response.status_code == 200, fmt
        assert response.headers["content-type"].startswith(ctype_prefix), fmt
        assert check(response), fmt


async def test_transcription_405_envelope_on_get_and_post_health(
    client: AsyncClient,
) -> None:
    """405 returns the project's error envelope, not the FastAPI default."""
    response = await client.get("/v1/audio/transcriptions")
    assert response.status_code == 405
    assert response.json()["error"]["type"] == "method_not_allowed_error"
    assert "GET" in response.json()["error"]["message"]
    assert "Allow" in response.headers

    response = await client.post("/health", data={})
    assert response.status_code == 405
    assert response.json()["error"]["type"] == "method_not_allowed_error"


async def test_transcription_file_too_large_400(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Upload exceeding max_upload_size_mb → 400."""
    big = b"x" * (26 * 1024 * 1024)
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("big.opus", big, "audio/opus")},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert "too large" in response.json()["error"]["message"].lower()


async def test_transcription_unsupported_format_400(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Invalid response_format → 400."""
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        data={"response_format": "xml"},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


async def test_transcription_warmup_503_takes_priority_over_auth(app: Any) -> None:
    """503 from ``ready=False`` fires before 401, by dependency order."""
    app.state.ready = False
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as warmup_client:
        response = await warmup_client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
            headers={"Authorization": "Bearer wrong-key"},
        )
    assert response.status_code == 503
    assert response.json()["error"]["type"] == "service_unavailable"
    assert response.headers["retry-after"] == "30"


async def test_registry_auth_active_and_complimentary_keys_authenticate(
    app: Any, client: AsyncClient
) -> None:
    """Active manual key + complimentary key both authenticate."""
    for kwargs in (
        {},
        {
            "customer_email": "comp@x.com",
            "billing_mode": "complimentary",
            "paid_until": None,
        },
    ):
        pt, _, _ = seed_customer_and_key(app.state.registry_db, **kwargs)  # type: ignore[arg-type]
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
            headers={"Authorization": f"Bearer {pt}"},
        )
        assert response.status_code == 200, kwargs


async def test_registry_auth_suspended_revoked_expired_all_401_with_hint(
    app: Any, client: AsyncClient
) -> None:
    """Suspended / revoked / expired-paid-until keys all 401 with the
    operator-contact hint piped through the envelope."""
    cases = [
        ({"status": "suspended"}, "suspended"),
        ({"status": "revoked"}, "revoked"),
        ({"paid_until": "2020-01-01"}, "suspended"),  # expired manual
    ]
    for kwargs, expected_phrase in cases:
        pt, _, _ = seed_customer_and_key(
            app.state.registry_db,
            customer_email=f"x{expected_phrase}@x.com",
            **kwargs,  # type: ignore[arg-type]
        )
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
            headers={"Authorization": f"Bearer {pt}"},
        )
        assert response.status_code == 401, expected_phrase
        body = response.json()
        assert expected_phrase in body["error"]["message"].lower()
        assert "Contact Ana" in body["error"]["hint"]


async def test_registry_auth_unknown_or_malformed_401_envelope(
    client: AsyncClient,
) -> None:
    """Unknown bearer + malformed Authorization headers → 401 with the
    enumeration-resistant envelope (same shape regardless of cause)."""
    for header in (
        "Bearer cmpd_live_" + "a" * 43,  # unknown valid-shape
        "",
        "Basic xyz",
        "Bearer",
        "BearerXYZ",
    ):
        response = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
            headers={"Authorization": header} if header else {},
        )
        assert response.status_code == 401, header
        body = response.json()
        assert body["error"]["type"] == "authentication_error"
        if "cmpd_live_" in header:
            assert body["error"]["message"] == "Invalid or missing API key"


async def test_no_audit_log_writes_on_any_transcription_outcome(
    app: Any, client: AsyncClient
) -> None:
    """Auth path NEVER writes to audit_log. The audit table is reserved
    for CLI mutations; the runtime hot path stays read-only."""
    conn: sqlite3.Connection = app.state.registry_db
    pt_ok, _, _ = seed_customer_and_key(conn)
    pt_susp, _, _ = seed_customer_and_key(
        conn, customer_email="x@y.com", status="suspended"
    )
    before = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    for headers in (
        {"Authorization": f"Bearer {pt_ok}"},
        {"Authorization": f"Bearer {pt_susp}"},
        {"Authorization": "Bearer unknown-token"},
    ):
        await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
            headers=headers,
        )
    after = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    assert after == before
