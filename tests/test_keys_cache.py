"""Tests for ``AuthCache`` — TTL + cross-process invalidation sentinel.

Lean by design (test-suite review 2026-04-25): per-counter assertions
collapsed; kept the core contracts (set→get round-trip, TTL purge,
sentinel-mtime triggers cross-process flush).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from compadre.keys.cache import AuthCache
from compadre.keys.models import AuthPayload


def _payload(**overrides: object) -> AuthPayload:
    fields: dict[str, object] = {
        "customer_id": "cust_01abc",
        "key_id": "key_01abc",
        "status": "active",
        "billing_mode": "manual",
        "rate_limit_rpm": 60,
        "paid_until": "2099-12-31",
    }
    fields.update(overrides)
    return AuthPayload(**fields)  # type: ignore[arg-type]


def test_auth_cache_set_get_round_trip_with_hit_miss_counters() -> None:
    """Happy path: set, get returns payload, counters track hits/misses."""
    cache = AuthCache()
    assert cache.get("missing") is None  # miss
    cache.set("h1", _payload())
    assert cache.get("h1") is not None  # hit

    snap = cache.snapshot()
    assert snap["entries"] == 1
    assert snap["hits"] == 1
    assert snap["misses"] == 1
    # No keys/values leak — snapshot is a counter dict, not a dump.
    assert {"keys", "values", "items"}.isdisjoint(snap.keys())


def test_auth_cache_ttl_expiration_purges_entry() -> None:
    """Entries expire after ``ttl`` seconds — verified via injected timer."""
    clock = {"now": 0.0}
    cache = AuthCache(maxsize=32, ttl=60, timer=lambda: clock["now"])
    cache.set("h1", _payload())
    assert cache.get("h1") is not None

    clock["now"] = 61.0
    assert cache.get("h1") is None
    assert cache.snapshot()["entries"] == 0


def test_auth_cache_invalidation_sentinel_flushes_on_mtime_advance(
    tmp_path: Path,
) -> None:
    """Cross-process invalidation: when a CLI verb (revoke-key,
    rotate-key, update-key) touches the sentinel, the FastAPI app's
    next ``cache.get()`` flushes — so a revoked key stops authenticating
    on the very next request, NOT after the 60s TTL elapses."""
    sentinel = tmp_path / "registry.db.invalidate"
    sentinel.touch()
    cache = AuthCache(invalidation_sentinel=sentinel)
    cache.set("h1", _payload())
    assert cache.get("h1") is not None

    # Advance mtime by 2 seconds (deterministic across filesystems).
    future = time.time() + 2.0
    os.utime(sentinel, (future, future))

    assert cache.get("h1") is None
    assert cache.snapshot()["entries"] == 0


def test_auth_cache_sentinel_appearing_first_time_flushes_cache(
    tmp_path: Path,
) -> None:
    """Edge case: sentinel didn't exist when AuthCache initialised; the
    FIRST CLI mutation creates it. The app's next get() must detect
    sentinel-now-present and flush."""
    sentinel = tmp_path / "registry.db.invalidate"
    cache = AuthCache(invalidation_sentinel=sentinel)  # absent
    cache.set("h1", _payload())
    assert cache.get("h1") is not None

    sentinel.touch()
    assert cache.get("h1") is None
