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
from pathlib import Path
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
        ("vtt", "text/vtt", lambda r: r.text.startswith("WEBVTT") or r.text == ""),
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


async def test_auth_enumeration_resistance_all_401_identical_envelope(
    app: Any, client: AsyncClient
) -> None:
    """Unknown key, malformed key, suspended key, revoked key, and expired
    key all return 401 with ``authentication_error`` type and an identical
    message for the unknown/malformed pair (enumeration resistance)."""
    conn: sqlite3.Connection = app.state.registry_db

    pt_unknown = "cmpd_live_" + "a" * 43
    pt_suspended, _, _ = seed_customer_and_key(
        conn, customer_email="sus-enum@x.com", status="suspended"
    )
    pt_revoked, _, _ = seed_customer_and_key(
        conn, customer_email="rev-enum@x.com", status="revoked"
    )

    wav_bytes = _make_wav_bytes()
    responses: dict[str, dict[str, object]] = {}

    for label, headers in (
        ("unknown", {"Authorization": f"Bearer {pt_unknown}"}),
        ("wrong_prefix", {"Authorization": "Bearer not-cmpd-token"}),
        ("suspended", {"Authorization": f"Bearer {pt_suspended}"}),
        ("revoked", {"Authorization": f"Bearer {pt_revoked}"}),
    ):
        resp = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", wav_bytes, "audio/wav")},
            headers=headers,
        )
        assert resp.status_code == 401, f"{label}: expected 401, got {resp.status_code}"
        responses[label] = resp.json()

    # All must have authentication_error type
    for label, body in responses.items():
        assert body["error"]["type"] == "authentication_error", (
            f"{label}: expected authentication_error, got {body['error']['type']}"
        )

    # Unknown and wrong_prefix both go through ApiKeyNotFoundError
    # → identical message (enumeration resistance)
    unknown_msg = responses["unknown"]["error"]["message"]
    wrong_prefix_msg = responses["wrong_prefix"]["error"]["message"]
    assert unknown_msg == wrong_prefix_msg, (
        f"unknown={unknown_msg!r} vs wrong_prefix={wrong_prefix_msg!r}"
    )

    # Suspended and revoked carry their specific messages (with contact hint)
    assert "suspended" in responses["suspended"]["error"]["message"].lower()
    assert "Contact Ana" in responses["suspended"]["error"]["hint"]
    assert "revoked" in responses["revoked"]["error"]["message"].lower()
    assert "Contact Ana" in responses["revoked"]["error"]["hint"]


async def test_auth_complimentary_key_ignores_paid_until(
    app: Any, client: AsyncClient
) -> None:
    """A complimentary key with an expired ``paid_until`` still passes
    auth (200). Complimentary keys are not gated by billing dates."""
    pt, _, _ = seed_customer_and_key(
        app.state.registry_db,
        customer_email="comp-expired@x.com",
        billing_mode="complimentary",
        paid_until="2020-01-01",  # expired
    )
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers={"Authorization": f"Bearer {pt}"},
    )
    assert response.status_code == 200, (
        f"Complimentary key with expired paid_until should still be 200, "
        f"got {response.status_code}: {response.text[:300]}"
    )


# ── Upload limit boundary tests ──────────────────────────────────────


async def test_transcription_duration_too_long_400(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio > 10 min → 400 with duration error message."""
    from compadre import api as api_module

    monkeypatch.setattr(api_module, "get_audio_duration", lambda _p: 601.0)

    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers=auth_headers,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "too long" in body["error"]["message"].lower() or (
        "601" in body["error"]["message"]
    )


async def test_transcription_duration_within_limit_200(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Audio ≤ 10 min → 200 (mocked duration)."""
    from compadre import api as api_module

    monkeypatch.setattr(api_module, "get_audio_duration", lambda _p: 599.0)

    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers=auth_headers,
    )
    # The engine is mocked, so transcription returns empty but 200
    assert response.status_code == 200


async def test_transcription_duration_at_boundary_599_passes_601_fails(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """599s passes, 601s fails — off-by-one at the 600s boundary."""
    from compadre import api as api_module

    monkeypatch.setattr(api_module, "get_audio_duration", lambda _p: 599.0)
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers=auth_headers,
    )
    assert response.status_code == 200

    monkeypatch.setattr(api_module, "get_audio_duration", lambda _p: 601.0)
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers=auth_headers,
    )
    assert response.status_code == 400
    assert "too long" in response.json()["error"]["message"].lower()


async def test_transcription_size_exactly_25_mb_200(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Exactly 25 MB file passes the size check."""
    from compadre import api as api_module

    # Mock duration + decode to bypass ffprobe/ffmpeg on x-bytes "audio"
    monkeypatch.setattr(api_module, "get_audio_duration", lambda _p: 5.0)
    monkeypatch.setattr(api_module, "decode_audio_to_wav", lambda *a, **kw: None)

    exact = b"x" * (25 * 1024 * 1024)
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("big.opus", exact, "audio/opus")},
        headers=auth_headers,
    )
    # size check passes; duration and decode pass (mocked); engine returns 200
    assert response.status_code == 200


async def test_transcription_size_25mb_plus_1_byte_400(
    client: AsyncClient,
    auth_headers: dict[str, str],
) -> None:
    """25 MB + 1 byte → 400."""
    over = b"x" * (25 * 1024 * 1024 + 1)
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("big.opus", over, "audio/opus")},
        headers=auth_headers,
    )
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"
    assert "too large" in body["error"]["message"].lower()


# ── Temp file cleanup verification ────────────────────────────────────


async def test_temp_files_cleaned_after_success(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """After a 200 transcription, no temp files leak on disk.

    We monkeypatch ``NamedTemporaryFile`` to record every created path
    and verify they are all unlinked after the request completes.
    """
    from tempfile import NamedTemporaryFile as OriginalNTF

    tracked: list[Path] = []

    class TrackedNTF:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._ntf = OriginalNTF(*args, **kwargs)  # noqa: SIM115
            path = Path(self._ntf.name)
            tracked.append(path)

        def __enter__(self) -> object:
            self._ntf.__enter__()
            return self._ntf

        def __exit__(self, *args: object) -> None:
            self._ntf.__exit__(*args)

        @property
        def name(self) -> str:
            return self._ntf.name

        def write(self, data: bytes) -> int:
            return self._ntf.write(data)

    import compadre.api as api_module

    monkeypatch.setattr(api_module, "NamedTemporaryFile", TrackedNTF)

    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers=auth_headers,
    )
    assert response.status_code == 200

    # All tracked temp files must be gone
    for tpath in tracked:
        assert not tpath.exists(), f"Temp file not cleaned up: {tpath}"


async def test_temp_files_cleaned_after_decode_failure(
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When ``decode_audio_to_wav`` raises, the input temp file must be
    cleaned up (no orphan tempfile on disk)."""
    from tempfile import NamedTemporaryFile as OriginalNTF

    from compadre import api as api_module

    tracked: list[Path] = []

    class TrackedNTF:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._ntf = OriginalNTF(*args, **kwargs)  # noqa: SIM115
            path = Path(self._ntf.name)
            tracked.append(path)

        def __enter__(self) -> object:
            self._ntf.__enter__()
            return self._ntf

        def __exit__(self, *args: object) -> None:
            self._ntf.__exit__(*args)

        @property
        def name(self) -> str:
            return self._ntf.name

        def write(self, data: bytes) -> int:
            return self._ntf.write(data)

    monkeypatch.setattr(api_module, "NamedTemporaryFile", TrackedNTF)

    def _fail_decode(*args: object, **kwargs: object) -> None:
        from compadre.exceptions import InvalidRequestError

        raise InvalidRequestError("simulated decode failure", hint="test")

    monkeypatch.setattr(api_module, "decode_audio_to_wav", _fail_decode)

    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers=auth_headers,
    )
    # decode failure → 400
    assert response.status_code == 400
    body = response.json()
    assert body["error"]["type"] == "invalid_request_error"

    for tpath in tracked:
        assert not tpath.exists(), f"Temp file not cleaned up: {tpath}"


async def test_temp_files_cleaned_after_transcription_timeout(
    app: Any,
    client: AsyncClient,
    auth_headers: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the engine raises ``TimeoutError`` (wrapped by the API as
    CompadreError 500), the WAV temp file must be cleaned up."""
    from tempfile import NamedTemporaryFile as OriginalNTF

    from compadre import api as api_module

    tracked: list[Path] = []

    class TrackedNTF:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self._ntf = OriginalNTF(*args, **kwargs)  # noqa: SIM115
            path = Path(self._ntf.name)
            tracked.append(path)

        def __enter__(self) -> object:
            self._ntf.__enter__()
            return self._ntf

        def __exit__(self, *args: object) -> None:
            self._ntf.__exit__(*args)

        @property
        def name(self) -> str:
            return self._ntf.name

        def write(self, data: bytes) -> int:
            return self._ntf.write(data)

    monkeypatch.setattr(api_module, "NamedTemporaryFile", TrackedNTF)

    # Mock decode + duration to succeed, then make the engine raise TimeoutError
    monkeypatch.setattr(api_module, "get_audio_duration", lambda _p: 5.0)
    monkeypatch.setattr(api_module, "decode_audio_to_wav", lambda *a, **kw: None)

    # Set the mock engine's transcribe to raise TimeoutError
    app.state.engine.transcribe.side_effect = TimeoutError("simulated timeout")

    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers=auth_headers,
    )
    # Timeout → CompadreError 500
    assert response.status_code == 500
    body = response.json()
    assert body["error"]["type"] == "server_error"
    assert "timed out" in body["error"]["message"].lower()

    for tpath in tracked:
        assert not tpath.exists(), f"Temp file not cleaned up: {tpath}"
