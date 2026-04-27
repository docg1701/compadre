"""Tests for ``compadre.rate_limit`` (per-key bucket + IP resolution).

Lean by design (test-suite review 2026-04-25): per-flag mock asserts
on ``check_rate_limit`` collapsed; happy + trip paths kept; X-Forwarded-For
parsing kept (multiple comma values + fallback).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.requests import Request

from compadre.exceptions import KeyRegistryError, RateLimitError
from compadre.keys.models import AuthPayload
from compadre.rate_limit import RateLimiter, check_rate_limit, get_client_ip


def _build_request(
    *,
    headers: dict[str, str] | None = None,
    client_host: str | None = None,
    auth_payload: AuthPayload | None = None,
    rate_limiter: RateLimiter | None = None,
) -> Request:
    raw_headers = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope: dict[str, Any] = {
        "type": "http",
        "method": "POST",
        "path": "/v1/audio/speech",
        "headers": raw_headers,
        "client": (client_host, 12345) if client_host else None,
        "app": SimpleNamespace(state=SimpleNamespace(rate_limiter=rate_limiter)),
    }
    request = Request(scope)
    if auth_payload is not None:
        request.state.auth_payload = auth_payload
    return request


def _payload(*, rate_limit_rpm: int = 60, key_id: str = "key_01hfx") -> AuthPayload:
    return AuthPayload(
        customer_id="cust_01hfx",
        key_id=key_id,
        status="active",
        billing_mode="manual",
        rate_limit_rpm=rate_limit_rpm,
        paid_until="2099-12-31",
    )


def test_get_client_ip_x_forwarded_for_leftmost_wins() -> None:
    """Caddy-injected XFF: leftmost (real client) wins over downstream
    proxies; whitespace stripped."""
    req = _build_request(
        headers={"x-forwarded-for": "  203.0.113.10  , 10.0.0.1, 10.0.0.2"},
        client_host="10.0.0.1",
    )
    assert get_client_ip(req) == "203.0.113.10"


def test_get_client_ip_falls_back_to_client_then_loopback() -> None:
    """No XFF → request.client.host; no client → 127.0.0.1."""
    assert get_client_ip(_build_request(client_host="198.51.100.5")) == "198.51.100.5"
    assert get_client_ip(_build_request()) == "127.0.0.1"


async def test_check_rate_limit_allows_up_to_rpm_then_429() -> None:
    """rpm=2: two requests pass, third trips RateLimitError."""
    rl = RateLimiter.build()
    payload = _payload(rate_limit_rpm=2)
    req = _build_request(auth_payload=payload, rate_limiter=rl)

    await check_rate_limit(req)  # 1
    await check_rate_limit(req)  # 2
    with pytest.raises(RateLimitError):
        await check_rate_limit(req)  # 3 trips


async def test_check_rate_limit_independent_buckets_per_key() -> None:
    """Two keys with same rate_limit_rpm bucket independently."""
    rl = RateLimiter.build()
    p1 = _payload(rate_limit_rpm=1, key_id="key_aaa")
    p2 = _payload(rate_limit_rpm=1, key_id="key_bbb")

    await check_rate_limit(_build_request(auth_payload=p1, rate_limiter=rl))
    # Key A is now at limit; key B still has its full budget.
    await check_rate_limit(_build_request(auth_payload=p2, rate_limiter=rl))
    # Key A's second request trips.
    with pytest.raises(RateLimitError):
        await check_rate_limit(_build_request(auth_payload=p1, rate_limiter=rl))


async def test_check_rate_limit_missing_state_raises_key_registry_error() -> None:
    """Defense-in-depth: missing auth_payload or rate_limiter is a 500
    (operator misconfiguration), NOT a 200 (that would silently disable
    rate limiting)."""
    rl = RateLimiter.build()
    # Missing auth_payload.
    req = _build_request(rate_limiter=rl)
    with pytest.raises(KeyRegistryError):
        await check_rate_limit(req)

    # Missing rate_limiter on app.state.
    req2 = _build_request(auth_payload=_payload())
    with pytest.raises(KeyRegistryError):
        await check_rate_limit(req2)
