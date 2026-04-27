"""In-memory TTL cache for auth payloads with cross-process invalidation.

Wraps ``cachetools.TTLCache`` so downstream code depends on this module
(and this module alone) for cache behaviour. A future swap (LRU, ARC,
per-replica vs shared) touches this file only.

The cache stores ``AuthPayload`` keyed by SHA-256 hex digest of the bearer
token — the same hash used for the DB lookup. No plaintext, no personal
data ever enters the cache.

Cross-process invalidation via a filesystem sentinel — the CLI process
(``compadre-keys update-key``, ``rotate-key``, ``revoke-key``) writes a
monotonic integer (``time.monotonic_ns()`` at the writer's clock) into
``{registry_db_path}.invalidate`` on every mutation. The API reads the
file's CONTENT (not mtime) on every cache read; if the integer advanced
since the last check, the whole cache is flushed.

F17 — content-based signaling sidesteps two failure modes that the
prior mtime-based design was vulnerable to:
  - **Coarse-mtime filesystems** (FAT32, some overlay FS) round mtime to
    1-second resolution; two CLI mutations within the same second would
    collide on a single mtime value and the second invalidation would
    be missed.
  - **Same-tick collisions** on fast SSDs where two ``touch`` calls
    inside one ns boundary share a single ``st_mtime_ns``.

Falls back to mtime-based detection if the file content cannot be parsed
(legacy installations that pre-date the content writer, or a corrupt
file). The fallback is loud-via-WARN once per process to alert operators.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from cachetools import TTLCache

from compadre.keys.models import AuthPayload

AUTH_CACHE_DEFAULT_MAXSIZE = 4096
AUTH_CACHE_DEFAULT_TTL_SECONDS = 60


class AuthCache:
    """TTL cache for ``AuthPayload`` values keyed by token hash.

    ``cachetools.TTLCache`` is not thread-safe on its own. The asyncio
    event loop is single-threaded so we don't need thread-safety today,
    but keeping this wrapper means any lock migration touches one module,
    not every caller.

    ``hits`` / ``misses`` counters are process-local telemetry for Story
    2.3's ``compadre-keys cache-status`` surface.
    """

    def __init__(
        self,
        *,
        maxsize: int = AUTH_CACHE_DEFAULT_MAXSIZE,
        ttl: int = AUTH_CACHE_DEFAULT_TTL_SECONDS,
        invalidation_sentinel: Path | None = None,
        timer: Any = None,
    ) -> None:
        if timer is None:
            self._cache: TTLCache[str, AuthPayload] = TTLCache(maxsize=maxsize, ttl=ttl)
        else:
            self._cache = TTLCache(maxsize=maxsize, ttl=ttl, timer=timer)
        self._hits = 0
        self._misses = 0
        self.maxsize = maxsize
        self.ttl = ttl
        self._sentinel = invalidation_sentinel
        # F17 — last observed monotonic counter from the sentinel file
        # content. Falls back to mtime when the content is unparseable.
        self._sentinel_token: int = self._read_sentinel_token()

    def _read_sentinel_token(self) -> int:
        """Return the sentinel's content (preferred) or its mtime_ns
        fallback, or 0 if absent.

        The writer (``_touch_auth_cache_sentinel``) writes an int. Older
        installations may have a touch-only file with no content; in that
        case we fall back to mtime so the invalidation contract still
        works (with the prior coarse-mtime caveat).
        """
        if self._sentinel is None:
            return 0
        try:
            text = self._sentinel.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return 0
        except OSError:
            # Permission denied / IO error — fall back to mtime.
            try:
                return self._sentinel.stat().st_mtime_ns
            except OSError:
                return 0
        if text:
            try:
                return int(text)
            except ValueError:
                # Pre-F17 zero-byte sentinel or corrupt content; fall
                # back to mtime so we still invalidate.
                pass
        try:
            return self._sentinel.stat().st_mtime_ns
        except OSError:
            return 0

    def _flush_if_invalidated(self) -> None:
        """Flush the cache if a CLI mutation advanced the sentinel token.

        Uses ``>`` rather than ``!=`` so clock-rollback (NTP step-back,
        container clock drift) doesn't trigger spurious cache flushes.
        F17 — content-based comparison avoids coarse-mtime same-second
        collisions; mtime fallback applies only to legacy / corrupt
        sentinels.
        """
        if self._sentinel is None:
            return
        current = self._read_sentinel_token()
        if current > self._sentinel_token:
            self._cache.clear()
            self._sentinel_token = current

    def get(self, key_hash: str) -> AuthPayload | None:
        self._flush_if_invalidated()
        payload = cast("AuthPayload | None", self._cache.get(key_hash))
        if payload is None:
            self._misses += 1
        else:
            self._hits += 1
        return payload

    def set(self, key_hash: str, payload: AuthPayload) -> None:
        self._cache[key_hash] = payload

    def clear(self) -> None:
        self._cache.clear()

    def snapshot(self) -> dict[str, Any]:
        """Return a read-only snapshot for telemetry.

        Story 2.3 reads this via ``compadre-keys cache-status --format json``.
        """
        return {
            "entries": len(self._cache),
            "maxsize": self.maxsize,
            "ttl_seconds": self.ttl,
            "hits": self._hits,
            "misses": self._misses,
        }
