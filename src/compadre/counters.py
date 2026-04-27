"""In-memory per-key request counters + periodic snapshot writer.

Story 2.3 (FR33 + FR45). Counts ``requests_total``, ``auth_success``,
``rate_limit_429``, ``peak_rpm``, and ``avg_rpm_last_60s`` per ``key_id``,
resetting on restart. A background asyncio task writes an atomic JSON
snapshot to ``{KEY_REGISTRY_DB_PATH}.counters.{replica_id}.json`` (one file
per replica) so the out-of-process ``compadre-keys`` CLI (``cache-status``,
``show --audit-tail``) can surface those counters without an internal HTTP
endpoint — the filesystem is the IPC channel, mirroring the ``.invalidate``
sentinel pattern from Story 2.1.

Multi-replica correctness: each replica writes its own file (the
``{replica_id}`` slug is ``socket.gethostname()`` by default — in Docker
that is the container ID, stable per replica and unique across replicas
sharing a bind-mounted registry volume). CLI readers glob every matching
file and aggregate, so counters on a N-replica deployment reflect the full
traffic, not just one replica's share.

Thread-safety model: the snapshot dict is built **on the asyncio event
loop** (``snapshot`` is a pure synchronous method — no ``await``), then
the pre-serialized JSON string is handed to ``asyncio.to_thread`` for the
file I/O. The worker thread never touches mutable in-memory state, so
there is no cross-thread race with ``incr_*`` calls happening on the loop.

Import graph — stdlib only (plus ``compadre.logging``). ``keys/auth.py`` and
``rate_limit.py`` import this module (downward); this module must NOT
import them, otherwise the carefully-layered downward-only rule collapses.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import tempfile
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from compadre.logging import get_logger

_logger = get_logger("counters")

SNAPSHOT_SCHEMA_VERSION = 1
DEFAULT_SNAPSHOT_INTERVAL_SECONDS = 15
WINDOW_SECONDS = 60


def _iso_utc_ms(dt: datetime) -> str:
    """Return ``dt`` as ``YYYY-MM-DDTHH:MM:SS.sssZ`` (UTC, millisecond precision).

    Mirrors the shape used throughout the snapshot schema so consumer code
    can parse every timestamp with a single grammar.
    """
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def default_replica_id() -> str:
    """Return the default per-replica snapshot identifier.

    Uses ``socket.gethostname()``: under Docker this is the container's
    short ID (stable per replica, unique across replicas on the same
    bind-mounted volume); on bare-metal single-process it's the machine
    hostname. Either way the value is stable across the process lifetime
    and distinct between replicas, which is the only requirement.
    """
    return socket.gethostname()


def counters_snapshot_path(db_path: Path | str, *, replica_id: str) -> Path:
    """Compose the per-replica snapshot path for ``db_path``.

    ``replica_id`` is embedded as a filename segment so the CLI can glob
    ``{db_path}.counters.*.json`` and aggregate across replicas.
    """
    return Path(f"{db_path}.counters.{replica_id}.json")


def counters_snapshot_glob(db_path: Path | str) -> tuple[Path, str]:
    """Return ``(parent_dir, glob_pattern)`` for enumerating snapshot files.

    The pattern matches every replica's snapshot for the given DB. Readers
    ``pathlib.Path(parent).glob(pattern)`` over the result.
    """
    base = Path(f"{db_path}.counters")
    return base.parent, f"{base.name}.*.json"


@dataclass
class _KeyCounter:
    """Per-``key_id`` counter bundle. Created lazily on first increment."""

    requests_total: int = 0
    auth_success: int = 0
    rate_limit_429: int = 0
    peak_rpm: int = 0
    window_hits: deque[float] = field(default_factory=deque)
    last_activity_wall: float = 0.0  # unix epoch, 0.0 = never

    def prune_window(self, *, now: float) -> None:
        """Drop timestamps older than ``WINDOW_SECONDS`` from the left of the deque."""
        cutoff = now - WINDOW_SECONDS
        hits = self.window_hits
        while hits and hits[0] < cutoff:
            hits.popleft()


@dataclass
class RequestCounters:
    """Process-lifetime counters for per-key request attribution.

    Lives on ``app.state.request_counters``. All increments are pure dict
    ops under the single asyncio event loop, so no locking is required —
    and the snapshot writer is careful to build its view on the loop too
    (not inside the worker thread), preserving the single-writer property.

    ``snapshot_path=None`` disables the on-disk snapshot writer entirely —
    used in tests that care only about the in-memory counters.
    """

    snapshot_path: Path | None = None
    snapshot_interval_seconds: int = DEFAULT_SNAPSHOT_INTERVAL_SECONDS
    per_key: dict[str, _KeyCounter] = field(default_factory=dict)
    process_start: float = field(default_factory=time.monotonic)

    _task: asyncio.Task[None] | None = field(default=None, init=False, repr=False)
    _auth_cache_snapshot: Callable[[], dict[str, Any]] | None = field(
        default=None, init=False, repr=False
    )

    def _counter(self, key_id: str) -> _KeyCounter:
        kc = self.per_key.get(key_id)
        if kc is None:
            kc = _KeyCounter()
            self.per_key[key_id] = kc
        return kc

    def incr_request(self, key_id: str) -> None:
        """Register one request for ``key_id`` — bumps total + moving window."""
        kc = self._counter(key_id)
        now = time.monotonic()
        kc.requests_total += 1
        kc.window_hits.append(now)
        kc.prune_window(now=now)
        window_len = len(kc.window_hits)
        if window_len > kc.peak_rpm:
            kc.peak_rpm = window_len
        kc.last_activity_wall = time.time()

    def incr_auth_success(self, key_id: str) -> None:
        """Register one successful auth for ``key_id``.

        Called after ``incr_request`` on the auth-success path. A 429 that
        lands after auth has already fired both ``incr_request`` and
        ``incr_auth_success`` — see AC2.
        """
        self._counter(key_id).auth_success += 1

    def incr_429(self, key_id: str) -> None:
        """Register one 429 (rate-limit denial) for ``key_id``.

        Does NOT bump ``requests_total`` — the request was already counted
        at the auth layer. Measuring this separately lets operators answer
        "how much of this key's traffic is getting throttled?" via a plain
        ratio.
        """
        self._counter(key_id).rate_limit_429 += 1

    def snapshot(self, auth_cache_snapshot: dict[str, Any]) -> dict[str, Any]:
        """Build the JSON-ready snapshot dict from current in-memory state.

        ``auth_cache_snapshot`` is passed in rather than read from a stored
        reference so this method stays pure: the caller decides which cache
        telemetry lands in the snapshot. The writer loop passes
        ``app.state.auth_cache.snapshot()``; tests pass whatever dict they
        want to assert against.

        MUST be called on the asyncio event loop (or in sync tests that
        don't run the loop). It iterates ``self.per_key`` without locks,
        relying on the cooperative-scheduling guarantee that no other
        coroutine runs during a synchronous block — that guarantee is
        broken if this method is invoked from a worker thread.
        """
        now = time.monotonic()
        per_key_out: dict[str, dict[str, Any]] = {}
        for key_id, kc in self.per_key.items():
            kc.prune_window(now=now)
            if kc.last_activity_wall:
                last_activity_at: str | None = _iso_utc_ms(
                    datetime.fromtimestamp(kc.last_activity_wall, UTC)
                )
            else:
                last_activity_at = None
            per_key_out[key_id] = {
                "requests_total": kc.requests_total,
                "auth_success": kc.auth_success,
                "rate_limit_429": kc.rate_limit_429,
                "peak_rpm": kc.peak_rpm,
                "avg_rpm_last_60s": len(kc.window_hits),
                "last_activity_at": last_activity_at,
            }
        return {
            "schema_version": SNAPSHOT_SCHEMA_VERSION,
            "written_at": _iso_utc_ms(datetime.now(UTC)),
            "process_uptime_seconds": int(now - self.process_start),
            "auth_cache": auth_cache_snapshot,
            "per_key": per_key_out,
        }

    def start_snapshot_writer(
        self,
        *,
        auth_cache_snapshot: Callable[[], dict[str, Any]],
    ) -> asyncio.Task[None]:
        """Schedule the periodic snapshot task on the running loop.

        ``auth_cache_snapshot`` is ``app.state.auth_cache.snapshot`` — taking
        the callable (not the ``AuthCache`` instance) keeps this module's
        import graph stdlib-only per AC20.
        """
        self._auth_cache_snapshot = auth_cache_snapshot
        loop = asyncio.get_running_loop()
        self._task = loop.create_task(self._writer_loop())
        return self._task

    async def stop_snapshot_writer(self) -> None:
        """Write a final snapshot, then cancel the writer task.

        Two writes in flight simultaneously are safe: ``_atomic_write``
        uses ``tempfile.mkstemp`` so each writer gets a unique tempfile
        and ``os.replace`` is per-file atomic. Last-writer-wins on the
        final inode but no torn output. Idempotent on double-stop.
        """
        task = self._task
        if task is None:
            return
        # Pre-cancel write: covers the common case of a writer sleeping
        # inside ``await asyncio.sleep(interval)`` at shutdown — we'd
        # otherwise lose up to one interval's worth of counters.
        await self._write_snapshot_safe()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None

    async def _writer_loop(self) -> None:
        """Background task body — sleep / write until cancelled."""
        try:
            while True:
                await asyncio.sleep(self.snapshot_interval_seconds)
                await self._write_snapshot_safe()
        except asyncio.CancelledError:
            raise

    async def _write_snapshot_safe(self) -> None:
        """Build snapshot on the loop, write in a worker thread.

        Split-phase design: step 1 iterates ``self.per_key`` synchronously
        on the event loop (no await → no interleaving with ``incr_*``);
        step 2 hands the pre-serialized JSON payload to ``asyncio.to_thread``
        for the blocking file I/O. The thread never touches mutable state.
        Any build or write exception is logged at WARN and swallowed —
        snapshots are advisory, never load-bearing.
        """
        if self.snapshot_path is None or self._auth_cache_snapshot is None:
            return
        try:
            data = self.snapshot(self._auth_cache_snapshot())
            payload = json.dumps(data, separators=(",", ":"))
        except Exception:
            _logger.warning(
                "Counter snapshot build failed for %s",
                self.snapshot_path,
                exc_info=True,
            )
            return
        try:
            await asyncio.to_thread(_atomic_write, self.snapshot_path, payload)
        except Exception:
            _logger.warning(
                "Counter snapshot write to %s failed",
                self.snapshot_path,
                exc_info=True,
            )

    def _write_snapshot_sync(self) -> None:
        """Sync test hook — build + write on the calling thread.

        Production uses :meth:`_write_snapshot_safe` which splits build
        (loop) vs. write (worker thread). Tests that want to drop a
        snapshot on disk without driving the event loop call this instead.
        """
        if self.snapshot_path is None or self._auth_cache_snapshot is None:
            return
        data = self.snapshot(self._auth_cache_snapshot())
        payload = json.dumps(data, separators=(",", ":"))
        _atomic_write(self.snapshot_path, payload)


def _atomic_write(path: Path, payload: str) -> None:
    """Atomically write ``payload`` (pre-serialized string) to ``path``.

    Uses ``tempfile.mkstemp`` to secure a unique tmp name per call — this
    eliminates torn-file races under concurrent writers (two shutdown
    paths, or two replicas sharing a volume, or the cancel handler racing
    a mid-sleep writer). ``chmod 0o600`` is applied to the tmp inode
    BEFORE ``os.replace``, so the mode follows the inode into the final
    position and every write lands at 0600 regardless of umask.

    Cleanup on failure: the tmp file is unlinked on exception so a
    permanently-full disk doesn't accumulate orphans.
    """
    parent = path.parent
    parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_str = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(parent),
    )
    tmp = Path(tmp_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(payload)
        tmp.chmod(0o600)
        tmp.replace(path)
    except Exception:
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
        raise


# ---------------------------------------------------------------------------
# Reader helpers — shared between ``compadre-keys cache-status`` and
# ``compadre-keys show --audit-tail``. Kept in this module so the CLI-side
# code stays DRY and the stdlib-only import rule still holds (no new deps).
# ---------------------------------------------------------------------------


def _parse_iso_utc(raw: str) -> datetime | None:
    """Parse an ISO 8601 UTC timestamp, tolerating both ``Z`` and ``+HH:MM`` forms.

    Naive results are coerced to UTC (a future writer variant that emits
    naive timestamps shouldn't crash the reader). Returns ``None`` on any
    parse failure.
    """
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _read_one_snapshot(path: Path) -> tuple[dict[str, Any], int] | None:
    """Read and parse a single per-replica snapshot file.

    Returns ``(data, age_seconds)`` on success, ``None`` on any failure
    (missing, unreadable, malformed JSON, missing/bad ``written_at``).
    Every failure is logged at WARN except ``FileNotFoundError`` (which is
    expected during concurrent reader / writer races over the glob).
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except OSError as exc:
        _logger.warning("Counters snapshot at %s unreadable: %s", path, exc)
        return None

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _logger.warning("Counters snapshot at %s is malformed JSON: %s", path, exc)
        return None

    if not isinstance(data, dict):
        _logger.warning("Counters snapshot at %s is not a JSON object", path)
        return None

    written_raw = data.get("written_at")
    if not isinstance(written_raw, str):
        return None
    written = _parse_iso_utc(written_raw)
    if written is None:
        _logger.warning(
            "Counters snapshot at %s has unparseable written_at=%r",
            path,
            written_raw,
        )
        return None
    age = max(0, int((datetime.now(UTC) - written).total_seconds()))
    return data, age


def read_all_snapshots(db_path: Path | str) -> list[tuple[dict[str, Any], int]]:
    """Enumerate every per-replica snapshot for ``db_path`` and parse each.

    Returns an empty list when no readable snapshots exist. Parse errors
    and missing files are logged and skipped — the CLI surface treats a
    partial read as "some replicas reporting" rather than a hard error.
    """
    parent, pattern = counters_snapshot_glob(db_path)
    try:
        paths = sorted(parent.glob(pattern))
    except OSError as exc:
        _logger.warning("Counters snapshot glob on %s failed: %s", parent, exc)
        return []
    results: list[tuple[dict[str, Any], int]] = []
    for path in paths:
        parsed = _read_one_snapshot(path)
        if parsed is not None:
            results.append(parsed)
    return results


@dataclass(frozen=True, slots=True)
class AggregatedCacheStatus:
    """Across-replica aggregate for the ``cache-status`` command.

    Fields:
      replicas: number of replicas whose snapshot parsed successfully
      entries: sum of per-replica cache entry counts (each replica has
               its own bounded LRU — totals indicate total working-set
               across the cluster)
      maxsize_per_replica: per-replica cache ceiling (assumed uniform —
                           reported as the max observed)
      hits / misses / lookups: sums across replicas
      hit_rate: hits / lookups (0.0 when lookups == 0)
      snapshot_age_seconds: minimum age — age of the freshest replica
      process_uptime_seconds: minimum uptime — "all replicas have been
                              collecting for at least this long"
    """

    replicas: int
    entries: int
    maxsize_per_replica: int
    hits: int
    misses: int
    lookups: int
    hit_rate: float
    snapshot_age_seconds: int
    process_uptime_seconds: int


def aggregate_cache_status(
    snapshots: list[tuple[dict[str, Any], int]],
) -> AggregatedCacheStatus | None:
    """Collapse per-replica snapshots into a single cache-health view.

    Returns ``None`` when ``snapshots`` is empty (no readable snapshots).
    Snapshot entries that fail type checks on individual fields are
    tolerated silently — aggregation degrades to "what parseable data is
    available" rather than all-or-nothing.
    """
    if not snapshots:
        return None
    hits = 0
    misses = 0
    entries = 0
    maxsize_max = 0
    uptime_min: int | None = None
    age_min = min(age for _, age in snapshots)
    for data, _age in snapshots:
        auth_cache = data.get("auth_cache")
        if isinstance(auth_cache, dict):
            hits += _coerce_int(auth_cache.get("hits"))
            misses += _coerce_int(auth_cache.get("misses"))
            entries += _coerce_int(auth_cache.get("entries"))
            maxsize = _coerce_int(auth_cache.get("maxsize"))
            if maxsize > maxsize_max:
                maxsize_max = maxsize
        uptime_raw = data.get("process_uptime_seconds")
        if isinstance(uptime_raw, (int, float)) and not isinstance(uptime_raw, bool):
            uptime = int(uptime_raw)
            uptime_min = uptime if uptime_min is None else min(uptime_min, uptime)
    lookups = hits + misses
    return AggregatedCacheStatus(
        replicas=len(snapshots),
        entries=entries,
        maxsize_per_replica=maxsize_max,
        hits=hits,
        misses=misses,
        lookups=lookups,
        hit_rate=(hits / lookups) if lookups else 0.0,
        snapshot_age_seconds=age_min,
        process_uptime_seconds=uptime_min if uptime_min is not None else 0,
    )


@dataclass(frozen=True, slots=True)
class AggregatedKeyStats:
    """Across-replica aggregate for one ``key_id``'s per-key counters.

    Semantics chosen so the numbers are honest under multi-replica load:
      requests_total / auth_success / rate_limit_429: summed (total traffic)
      peak_rpm: max across replicas (highest burst observed anywhere)
      avg_rpm_last_60s: summed (current global rate across replicas)
      last_activity_at: most recent (max) across replicas
      snapshot_age_seconds: min age (freshest replica reporting this key)
      process_uptime_seconds: min uptime (most conservative window)
      replicas: number of replicas that reported this key
    """

    requests_total: int
    auth_success: int
    rate_limit_429: int
    peak_rpm: int
    avg_rpm_last_60s: int
    last_activity_at: str | None
    snapshot_age_seconds: int
    process_uptime_seconds: int
    replicas: int


def aggregate_key_stats(
    snapshots: list[tuple[dict[str, Any], int]],
    key_id: str,
) -> AggregatedKeyStats | None:
    """Collapse per-replica per-key counters for ``key_id``.

    Returns ``None`` when no replica reports this ``key_id`` (the key has
    not served a request since the last restart on any replica).
    """
    contributing: list[tuple[dict[str, Any], int, int]] = []
    for data, age in snapshots:
        per_key = data.get("per_key")
        if not isinstance(per_key, dict):
            continue
        key_entry = per_key.get(key_id)
        if not isinstance(key_entry, dict):
            continue
        uptime_raw = data.get("process_uptime_seconds")
        uptime = _coerce_int(uptime_raw) if isinstance(uptime_raw, (int, float)) else 0
        contributing.append((key_entry, age, uptime))
    if not contributing:
        return None

    entries = [e for e, _, _ in contributing]
    return AggregatedKeyStats(
        requests_total=sum(_coerce_int(e.get("requests_total")) for e in entries),
        auth_success=sum(_coerce_int(e.get("auth_success")) for e in entries),
        rate_limit_429=sum(_coerce_int(e.get("rate_limit_429")) for e in entries),
        peak_rpm=max(_coerce_int(e.get("peak_rpm")) for e in entries),
        avg_rpm_last_60s=sum(_coerce_int(e.get("avg_rpm_last_60s")) for e in entries),
        last_activity_at=_max_iso_utc(e.get("last_activity_at") for e in entries),
        snapshot_age_seconds=min(age for _, age, _ in contributing),
        process_uptime_seconds=min(u for _, _, u in contributing),
        replicas=len(contributing),
    )


def _coerce_int(value: Any) -> int:
    """Coerce JSON scalars to ``int``, defaulting to 0 on anything weird.

    ``None``, booleans, non-numeric strings, and missing keys all collapse
    to 0. The aggregation surface is advisory; a defensive 0 is safer than
    a ``TypeError`` that crashes a reader mid-render.
    """
    if isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return 0


def _max_iso_utc(candidates: Any) -> str | None:
    """Return the most recent ISO-UTC string among ``candidates``, or ``None``.

    Parses each with :func:`_parse_iso_utc`; ignores values that don't
    parse. Returns the original string of the latest candidate so the
    caller can render it verbatim without another round-trip.
    """
    best: tuple[datetime, str] | None = None
    for candidate in candidates:
        if not isinstance(candidate, str):
            continue
        parsed = _parse_iso_utc(candidate)
        if parsed is None:
            continue
        if best is None or parsed > best[0]:
            best = (parsed, candidate)
    return best[1] if best is not None else None
