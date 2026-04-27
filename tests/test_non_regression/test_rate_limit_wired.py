"""FR50 non-regression — rate-limit gate wiring on /v1/audio/transcriptions.

(Story 4.5e).

Asserts the failure modes that would reintroduce the edfcb33-class bug
(rate-limit infrastructure wired but not actually enforced on request):

1. Route's dependency tree includes the canonical ``check_rate_limit`` object.
2. 3rd request at ``rate_limit_rpm=2`` returns 429 with the project envelope.
3. ``/health`` remains UN-rate-limited (CLAUDE.md design decision).

Uses the repo-level ASGI fixtures (no docker required).
"""

from __future__ import annotations

from datetime import date
from typing import Any

import pytest
from fastapi.routing import APIRoute
from httpx import AsyncClient

from compadre.keys import auth as auth_module
from compadre.rate_limit import check_rate_limit
from tests._keys_fixtures import seed_customer_and_key

_SPEECH_PATH = "/v1/audio/transcriptions"
_HEALTH_PATH = "/health"
_TEST_RPM = 2
_SPEECH_BODY = {
    "response_format": "text",
}


@pytest.mark.non_regression
def test_check_rate_limit_is_canonical_dep_on_speech_route() -> None:
    """FR50: ``Depends(check_rate_limit)`` must be on the POST /v1/audio/transcriptions
    route AND must resolve to the canonical ``compadre.rate_limit.check_rate_limit``
    object (not a same-named stub kept around after a refactor).

    Catches edfcb33-class regressions: rate-limit infrastructure wired but
    not actually enforced. Both the declared-list check and the identity
    check are needed — declared-list catches "Depends() removed", identity
    catches "renamed and old symbol left as no-op".
    """
    from compadre.main import create_app

    app = create_app()
    speech_routes = [
        r
        for r in app.routes
        if isinstance(r, APIRoute) and r.path == _SPEECH_PATH and "POST" in r.methods
    ]
    assert len(speech_routes) == 1, (
        f"Found {len(speech_routes)} POST {_SPEECH_PATH} routes"
    )
    route = speech_routes[0]

    declared = [d.dependency for d in (route.dependencies or [])]
    resolved = [d.call for d in route.dependant.dependencies]
    assert check_rate_limit in declared, (
        f"FR50: check_rate_limit not in declared deps: "
        f"{[getattr(c, '__name__', repr(c)) for c in declared]}"
    )
    resolved_labels = [
        (getattr(c, "__module__", "?"), getattr(c, "__name__", repr(c)))
        for c in resolved
    ]
    assert any(d is check_rate_limit for d in resolved), (
        f"FR50: route resolves a non-canonical check_rate_limit shim: {resolved_labels}"
    )


@pytest.mark.non_regression
async def test_speech_endpoint_emits_429_after_rate_limit(
    app: Any, client: AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR50: exceeding rate_limit_rpm on /v1/audio/transcriptions returns 429 with
    the project envelope + Retry-After + X-RateLimit-* headers.

    Most direct assertion of FR50 intent. If route inspection passes but
    this fails, something subtler is broken (check_rate_limit body always
    allows; MovingWindowRateLimiter.hit monkey-patched in a refactor).
    """
    monkeypatch.setattr(
        auth_module, "_today_in_billing_tz", lambda _s: date(2026, 4, 17)
    )
    plaintext, _, _ = seed_customer_and_key(
        app.state.registry_db, rate_limit_rpm=_TEST_RPM
    )
    headers = {"Authorization": f"Bearer {plaintext}"}

    import io
    import wave

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 16000)
    wav = buf.getvalue()
    for _ in range(_TEST_RPM):
        r = await client.post(
            _SPEECH_PATH,
            files={"file": ("test.wav", wav, "audio/wav")},
            data=_SPEECH_BODY,
            headers=headers,
        )
        assert r.status_code == 200, f"under-budget request 429'd: {r.text[:200]}"

    trip = await client.post(
        _SPEECH_PATH,
        files={"file": ("test.wav", wav, "audio/wav")},
        data=_SPEECH_BODY,
        headers=headers,
    )
    assert trip.status_code == 429, f"over-budget request: {trip.text[:200]}"

    # Retry-After: 1..61 (60s window + ceil-on-float-drift slack).
    retry_after = int(trip.headers["retry-after"])
    assert 1 <= retry_after <= 61, retry_after
    assert trip.json() == {
        "error": {
            "message": "Rate limit exceeded",
            "type": "rate_limit_error",
            "hint": f"Too many requests. Try again in {retry_after} seconds.",
        }
    }
    assert trip.headers.get("x-ratelimit-limit") == str(_TEST_RPM)
    assert trip.headers.get("x-ratelimit-remaining") == "0"
    reset_raw = trip.headers.get("x-ratelimit-reset")
    assert reset_raw is not None and int(reset_raw) > 0


@pytest.mark.non_regression
async def test_health_endpoint_is_not_rate_limited(
    app: Any, client: AsyncClient
) -> None:
    """CLAUDE.md design decision: /health is UN-rate-limited at the app
    layer. Operators rely on un-throttled probes for liveness checks; a
    future PR adding Depends(check_rate_limit) to /health is a design
    regression."""
    statuses = [(await client.get(_HEALTH_PATH)).status_code for _ in range(5)]
    assert 429 not in statuses, f"/health returned 429: {statuses}"
    assert all(s in {200, 503} for s in statuses), statuses
