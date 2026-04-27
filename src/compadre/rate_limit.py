"""Per-key rate limiting via the ``limits`` library.

``POST /v1/audio/transcriptions`` enforces one rate-limit bucket per API
key, sourced from the registry row's ``rate_limit_rpm`` column. No per-IP
bucket in the application — reverse-proxy (Caddy) handles any IP-level
hygiene.

Flow:
    verify_api_key (dependency)   → sets request.state.auth_payload
    ↓
    check_rate_limit (dependency) → .hit() on key:<key_id> bucket;
                                    writes X-RateLimit-* headers on allow
    ↓
    route handler                 → runs only if both pass

The limiter lives on ``app.state.rate_limiter`` (set in lifespan) so tests
can reset the storage between cases without touching a module-level
singleton. Multi-replica caveat — in-memory storage means per-replica
buckets; see ``docs/rate-limiting.md``.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from limits import parse
from limits.aio.storage import MemoryStorage
from limits.aio.strategies import MovingWindowRateLimiter
from starlette.requests import Request

from compadre.exceptions import KeyRegistryError, RateLimitError
from compadre.keys.models import AuthPayload
from compadre.logging import get_logger

if TYPE_CHECKING:
    from compadre.counters import RequestCounters

_rate_limit_logger = get_logger("rate_limit")


@dataclass
class RateLimiter:
    """Bundle of the ``limits`` storage + strategy held on ``app.state``."""

    storage: MemoryStorage
    strategy: MovingWindowRateLimiter

    @classmethod
    def build(cls) -> RateLimiter:
        storage = MemoryStorage()
        return cls(storage=storage, strategy=MovingWindowRateLimiter(storage))

    async def reset(self) -> None:
        """Clear all buckets. Used by the test suite between cases."""
        await self.storage.reset()


def get_client_ip(request: Request) -> str:
    """Return the real client IP, trusting X-Forwarded-For from Caddy.

    Consumed by the access-log middleware only. Caddy injects
    X-Forwarded-For on every reverse-proxy hop; its leftmost entry is the
    original client. Falls back to ``request.client.host`` when the header
    is absent (direct local runs without a proxy).
    """
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    if request.client is not None:
        return request.client.host
    return "127.0.0.1"


# F14 — per-IP bucket on /health to bound the shape-valid-random DoS
# surface. Authenticated probes still use the auth path; this gate trips
# only when one IP exceeds 60 calls/minute, regardless of bearer
# validity. Operator-overridable via env if a noisy monitoring agent
# legitimately exceeds it. F20 — same per-IP bucket is reused on the
# pre-auth surface of /v1/audio/transcriptions so denied-key spam can't
# bypass the per-key bucket (auth runs before per-key rate-limit).
_HEALTH_PER_IP_LIMIT = "60/minute"
_SPEECH_PER_IP_LIMIT = "600/minute"


async def _check_per_ip_bucket(
    request: Request, *, limit_str: str, bucket_prefix: str
) -> None:
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        # Lifespan has not yet attached the limiter (test stubs that
        # bypass lifespan). Fail open — protecting against a never-running
        # limiter would block every request. Lifespan attaches the
        # limiter on startup; a missing limiter in production is a
        # framework bug, not a request-time concern.
        return
    ip = get_client_ip(request)
    limit = parse(limit_str)
    bucket = f"{bucket_prefix}:{ip}"
    allowed = await limiter.strategy.hit(limit, bucket)
    if allowed:
        return
    reset_time, _remaining = await limiter.strategy.get_window_stats(limit, bucket)
    now = time.time()
    retry_after = max(1, math.ceil(reset_time - now))
    reset_header = max(int(reset_time), int(now) + 1)
    _rate_limit_logger.info(
        "Per-IP rate-limit 429 | bucket=%s | ip=%s | limit=%s",
        bucket_prefix,
        ip,
        limit_str,
    )
    rpm = int(limit_str.split("/")[0])
    raise RateLimitError(
        retry_after=retry_after,
        limit=rpm,
        remaining=0,
        reset=reset_header,
    )


async def check_health_per_ip_rate_limit(request: Request) -> None:
    """F14 — per-IP rate-limit gate on /health. 60/min/IP."""
    await _check_per_ip_bucket(
        request, limit_str=_HEALTH_PER_IP_LIMIT, bucket_prefix="health-ip"
    )


async def check_transcription_per_ip_rate_limit(request: Request) -> None:
    """F20 — per-IP rate-limit gate on /v1/audio/transcriptions. 600/min/IP.

    Runs BEFORE ``verify_api_key`` so a denied-key spammer cannot bypass
    the per-key bucket by never authenticating. The 600/min headroom is
    well above any legitimate single-IP usage (a 10x margin over the
    default 60-rpm per-key limit) so a NAT-shared client cluster is
    unaffected; the bucket trips only on bot-shaped traffic.
    """
    await _check_per_ip_bucket(
        request, limit_str=_SPEECH_PER_IP_LIMIT, bucket_prefix="transcription-ip"
    )


async def check_rate_limit(request: Request) -> None:
    """FastAPI dependency — enforce the per-key rate limit.

    Reads ``request.state.auth_payload`` (populated by ``verify_api_key``)
    and ``request.app.state.rate_limiter``. On allow, stashes the current
    ``(limit, remaining, reset)`` on ``request.state.rate_limit_headers``
    so ``apply_rate_limit_headers_middleware`` (in ``main.py``) can write
    ``X-RateLimit-*`` onto the outbound response. Stashing on state —
    not on an injected FastAPI ``Response`` — is deliberate: when the
    route handler returns its own ``Response`` object, FastAPI does NOT merge
    dependency-injected
    response headers onto the final response, so the middleware is the
    only reliable write channel.

    On deny, raises ``RateLimitError`` carrying the same three fields plus
    a dynamically-computed ``Retry-After`` (seconds until the oldest hit
    in the window falls off).

    The bucket identifier is ``key:<key_id>`` — stable across ``rotate-key``
    (same ``key_id``, new plaintext), so a rotated key's minute-window
    inherits the pre-rotation counts. Zero PII in the bucket key.

    Must be declared AFTER ``Depends(verify_api_key)`` so that
    ``request.state.auth_payload`` is guaranteed set when this runs.
    """
    payload: AuthPayload | None = getattr(request.state, "auth_payload", None)
    if payload is None:
        raise KeyRegistryError(
            "Authentication did not populate request.state.auth_payload; "
            "check dependency ordering on the route"
        )
    limiter: RateLimiter | None = getattr(request.app.state, "rate_limiter", None)
    if limiter is None:
        raise KeyRegistryError(
            "RateLimiter not initialised on app.state; check lifespan startup"
        )
    rpm = payload.rate_limit_rpm
    if rpm < 1:
        raise KeyRegistryError(
            f"Invalid rate_limit_rpm={rpm} for key_id={payload.key_id}; "
            "DB CHECK enforces > 0, row may be corrupt"
        )
    limit = parse(f"{rpm}/minute")
    bucket = f"key:{payload.key_id}"
    allowed = await limiter.strategy.hit(limit, bucket)
    reset_time, remaining = await limiter.strategy.get_window_stats(limit, bucket)
    now = time.time()
    # ``math.ceil`` (not ``int``) so a sub-second remainder rounds UP — a
    # client that retries exactly at ``Retry-After`` must find the bucket
    # open, not hit a second 429 from the truncated seconds-off.
    retry_after = max(1, math.ceil(reset_time - now))
    # Clamp ``reset`` to a future epoch so clock-skew or stale windows
    # can't emit a past-unix-timestamp header that clients interpret as
    # "budget is available now."
    reset_header = max(int(reset_time), int(now) + 1)
    if not allowed:
        # Story 2.3 — register the denial against per-key counters and emit
        # a structured log line carrying ``key_id`` + ``customer_id``. Access
        # log already records the request at the HTTP layer; this line adds
        # the attribution an operator needs to grep "which customer is
        # hammering us?" without joining the HTTP log against the registry.
        # No log line on allow (hot path; access log is sufficient).
        counters: RequestCounters | None = getattr(
            request.app.state, "request_counters", None
        )
        if counters is not None:
            counters.incr_429(payload.key_id)
        _rate_limit_logger.info(
            "Rate-limit 429 | key_id=%s | customer_id=%s | rpm=%d",
            payload.key_id,
            payload.customer_id,
            rpm,
        )
        raise RateLimitError(
            retry_after=retry_after,
            limit=rpm,
            remaining=0,
            reset=reset_header,
        )
    # Clamp ``remaining`` to 0 — the ``hit`` → ``get_window_stats`` gap
    # can let a concurrent hit drive ``remaining`` negative via unclamped
    # ``amount - window_items`` inside ``limits``.
    request.state.rate_limit_headers = {
        "X-RateLimit-Limit": str(rpm),
        "X-RateLimit-Remaining": str(max(0, remaining)),
        "X-RateLimit-Reset": str(reset_header),
    }
