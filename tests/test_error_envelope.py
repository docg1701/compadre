"""Error envelope contract tests: every error response MUST have the
canonical shape ``{"error": {"message": str, "type": str, "hint": str}}``.

These are **mocked** tests (no real model needed) — they exercise the
exception handlers directly through the FastAPI ASGI client.
"""

from __future__ import annotations

import io
import wave
from typing import Any

from httpx import ASGITransport, AsyncClient, Response

from tests._keys_fixtures import seed_customer_and_key


def assert_error_envelope(
    response: Response,
    *,
    expected_type: str,
    expected_status: int,
) -> dict[str, object]:
    """Assert the canonical error envelope shape on a response.

    Returns the parsed JSON body for further field-level assertions.
    """
    assert response.status_code == expected_status, (
        f"Expected {expected_status}, got {response.status_code}: {response.text[:300]}"
    )
    body = response.json()
    assert "error" in body, f"Response missing 'error' key: {body}"
    error = body["error"]
    assert isinstance(error, dict)
    for key in ("message", "type", "hint"):
        assert key in error, f"Error envelope missing '{key}' key"
        assert isinstance(error[key], str), (
            f"Error.{key} must be str, got {type(error[key]).__name__}"
        )
    assert error["type"] == expected_type, (
        f"Expected type={expected_type!r}, got {error['type']!r}"
    )
    return body


def _make_wav_bytes(duration_ms: int = 1000) -> bytes:
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


# ── 400 — InvalidRequestError ─────────────────────────────────────────


async def test_error_envelope_400_invalid_request(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """Invalid response_format → 400 with canonical envelope."""
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        data={"response_format": "xml"},
        headers=auth_headers,
    )
    assert_error_envelope(
        response, expected_type="invalid_request_error", expected_status=400
    )


# ── 401 — all auth failures ───────────────────────────────────────────


async def test_error_envelope_401_missing_auth(
    client: AsyncClient,
) -> None:
    """Missing Authorization → 401 authentication_error."""
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
    )
    assert_error_envelope(
        response, expected_type="authentication_error", expected_status=401
    )


async def test_error_envelope_401_unknown_key(
    client: AsyncClient,
) -> None:
    """Unknown key hash → 401 authentication_error."""
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers={"Authorization": "Bearer cmpd_live_" + "a" * 43},
    )
    assert_error_envelope(
        response, expected_type="authentication_error", expected_status=401
    )


async def test_error_envelope_401_suspended_key(app: Any, client: AsyncClient) -> None:
    """Suspended key → 401 authentication_error with hint."""
    pt, _, _ = seed_customer_and_key(
        app.state.registry_db, customer_email="env401@s.com", status="suspended"
    )
    response = await client.post(
        "/v1/audio/transcriptions",
        files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        headers={"Authorization": f"Bearer {pt}"},
    )
    body = assert_error_envelope(
        response, expected_type="authentication_error", expected_status=401
    )
    assert "suspended" in body["error"]["hint"].lower()


# ── 404 — not_found_error ────────────────────────────────────────────


async def test_error_envelope_404_not_found(
    client: AsyncClient,
) -> None:
    """GET /nonexistent → 404 not_found_error with canonical envelope."""
    response = await client.get("/nonexistent")
    body = assert_error_envelope(
        response, expected_type="not_found_error", expected_status=404
    )
    assert "not found" in body["error"]["message"].lower()


# ── 405 — method_not_allowed_error ───────────────────────────────────


async def test_error_envelope_405_method_not_allowed(
    client: AsyncClient,
) -> None:
    """GET /v1/audio/transcriptions → 405 with canonical envelope + Allow header."""
    response = await client.get("/v1/audio/transcriptions")
    body = assert_error_envelope(
        response, expected_type="method_not_allowed_error", expected_status=405
    )
    assert "GET" in body["error"]["message"]
    assert "Allow" in response.headers


# ── 429 — rate_limit_error ───────────────────────────────────────────


async def test_error_envelope_429_rate_limit(
    app: Any,
    client: AsyncClient,
) -> None:
    """RateLimitError → 429 with canonical envelope + Retry-After header.

    We force the error by seeding a key with ``rpm=1`` and firing two
    requests; the second trips the limit.
    """
    import io
    import wave
    from datetime import date

    from compadre.keys import auth as auth_module
    from tests._keys_fixtures import seed_customer_and_key

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 16000)
    wav = buf.getvalue()

    # Freeze billing date so the seeded key stays active
    orig_today = getattr(auth_module, "_today_in_billing_tz", None)
    auth_module._today_in_billing_tz = lambda _s: date(2026, 4, 17)
    try:
        plaintext, _, _ = seed_customer_and_key(app.state.registry_db, rate_limit_rpm=1)
        headers = {"Authorization": f"Bearer {plaintext}"}

        # First request passes
        r1 = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", wav, "audio/wav")},
            headers=headers,
        )
        assert r1.status_code == 200, f"under-budget request 429'd: {r1.text[:200]}"

        # Second request trips
        r2 = await client.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", wav, "audio/wav")},
            headers=headers,
        )
    finally:
        if orig_today is not None:
            auth_module._today_in_billing_tz = orig_today

    body = assert_error_envelope(
        r2, expected_type="rate_limit_error", expected_status=429
    )
    assert "rate limit" in body["error"]["message"].lower()
    assert "retry-after" in r2.headers
    retry_after = int(r2.headers["retry-after"])
    assert 1 <= retry_after <= 61
    assert r2.headers.get("x-ratelimit-limit") == "1"
    assert r2.headers.get("x-ratelimit-remaining") == "0"
    assert int(r2.headers.get("x-ratelimit-reset", 0)) > 0


# ── 500 — server_error (unhandled) ───────────────────────────────────


async def test_error_envelope_500_server_error() -> None:
    """Unhandled Exception → 500 server_error, no raw traceback in body.

    Tests the ``unhandled_error_handler`` directly by calling it with a
    mock request. The actual FastAPI routing through ASGI can be tricky
    because Starlette's ``ServerErrorMiddleware`` interacts differently
    with custom ``Exception`` handlers depending on debug mode.
    """
    from starlette.requests import Request

    from compadre.exceptions import unhandled_error_handler

    scope: dict[str, object] = {
        "type": "http",
        "method": "GET",
        "path": "/trigger-500",
        "headers": [],
    }
    request = Request(scope)
    exc = RuntimeError("simulated unhandled error")

    response = await unhandled_error_handler(request, exc)

    assert response.status_code == 500
    body: dict[str, object] = response.body  # type: ignore[assignment]
    import json

    parsed = json.loads(body) if isinstance(body, bytes) else body
    error = parsed["error"]
    assert error["type"] == "server_error"
    assert error["message"] == "An internal server error occurred"
    assert error["hint"] == "Check server logs for the full traceback."
    # No raw traceback in message
    assert "simulated" not in error["message"]
    assert "Traceback" not in error["message"]


# ── 503 — service_unavailable ────────────────────────────────────────


async def test_error_envelope_503_service_unavailable(
    app: Any,
) -> None:
    """ServiceUnavailableError → 503 with canonical envelope + Retry-After header."""
    app.state.ready = False
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        response = await c.post(
            "/v1/audio/transcriptions",
            files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
        )
    assert_error_envelope(
        response, expected_type="service_unavailable", expected_status=503
    )
    assert response.headers["retry-after"] == "30"
