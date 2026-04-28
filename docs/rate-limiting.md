# Rate Limiting

> **Last updated:** 2026-04-26

# How Rate Limiting Works

Compadre enforces one rate-limit bucket per API key. The per-key cap lives in the registry (`api_key.rate_limit_rpm`) and is consulted on every authenticated request. There is no per-IP rate limit at the application layer and no global toggle — the registry row is authoritative.

## Design rationale

- **Per-key, not per-IP.** Rate-limit is a billing lever, not a firewall. Each customer buys a tier by way of an API key with a specific `rate_limit_rpm`. An IP-based limit would cap all customers sharing an egress NAT, and would not differentiate paid tiers.
- **No reverse-proxy handoff.** Caddy is free to add connection-level protections (concurrency caps, IP blocks, TLS) but the per-key business logic lives in the application where the registry is reachable.
- **In-memory storage.** Bucket state is per-replica in RAM. This removes an external dependency (no Redis, no shared SQLite write contention on the hot path) at the cost of per-replica enforcement semantics — documented below.

## Algorithm

- **Library**: [`limits`](https://limits.readthedocs.io/) — an algorithm-agnostic rate-limit toolkit used directly as a FastAPI dependency (no middleware wrapper).
- **Strategy**: `limits.aio.strategies.MovingWindowRateLimiter` — sliding 1-minute window. Each `.hit()` call adds a timestamped event to the bucket and returns `True` only if the count in the last 60 seconds would remain within `rate_limit_rpm` after the hit.
- **Storage**: `limits.aio.storage.MemoryStorage` — plain in-process dictionaries. No fsync, no network round-trip.
- **Wiring**: constructed once in the lifespan (`RateLimiter.build()`) and stored on `app.state.rate_limiter`. Tests reset buckets between cases via `await app.state.rate_limiter.reset()`.
- **Dependency ordering**: `check_rate_limit` is a FastAPI dependency declared **after** `verify_api_key`. When it runs, `request.state.auth_payload` is guaranteed populated, so the `rate_limit_rpm` and `key_id` values are read from there — no second DB lookup on the hot path.

Pseudo-flow for `POST /v1/audio/transcriptions`:

```text
verify_api_key(request)              → sets request.state.auth_payload
check_rate_limit(request, response)  → limiter.hit(key:<key_id>, rpm)
    allowed?  True  → write X-RateLimit-* headers onto response,
                      handler runs, STT engine transcribes audio
    allowed?  False → raise RateLimitError(retry_after, limit, remaining, reset)
route handler (transcribe_audio)
```

## Bucket identity

Each bucket is keyed on the string `key:<key_id>` where `<key_id>` is the registry row's ULID-based identifier (e.g. `key_01jxy...`).

Consequences of this choice:

- **Rotation preserves the bucket.** `compadre-keys rotate-key` swaps the plaintext and the SHA-256 hash but keeps the same `key_id`. A customer exhausting their budget and then rotating cannot reset the window — the next request lands on the same counter.
- **Buckets are per key, not per customer.** A customer with two active keys (same `customer_id`, different `key_id`) gets two independent buckets. This is intentional: operationally two keys often represent two distinct integrations and should be sized independently.
- **No PII in the bucket key.** `key_id` is an opaque ULID — the bucket key is safe to log at debug level if needed.
- **IP is not part of the bucket.** Two clients sharing a plaintext share the bucket.

## Multi-replica behaviour

`MemoryStorage` is per-process. Today Compadre ships `replicas: 1` in `compose.yaml`, so the in-memory window is the global window. If `replicas` is raised to N:

- Each replica keeps its own moving window for each key.
- A key with `rate_limit_rpm=60` can emit up to `60 × N` requests per minute cluster-wide (in the worst-case distribution where Caddy's `least_conn` policy spreads traffic perfectly).
- The counter on each replica resets when that replica restarts.

This trade-off is honest and visible: the alternative (shared storage) trades a simple deploy for an extra dependency (Redis) or a write-contention-prone shared SQLite table. For Compadre's current throughput and replica count, the in-memory path is the right choice. Moving to shared storage is a candidate future story if and when per-replica multiplication becomes a real operator problem.

If you scale up and need closer-to-strict per-key caps, divide intended RPM by replica count when setting `compadre-keys update-key --rpm <n>`, or accept the multiplied budget as opportunistic elasticity.

## Response headers

Every authenticated response carries the client's current view of its bucket:

| Header | Meaning |
| --- | --- |
| `X-RateLimit-Limit` | The key's `rate_limit_rpm` at request time (integer). |
| `X-RateLimit-Remaining` | Requests still available in the current moving window (integer). |
| `X-RateLimit-Reset` | Unix epoch seconds at which the oldest counted hit falls off the window (integer). |

On a 429 the same three headers are emitted alongside `Retry-After`, so clients can back off accurately without polling. `Retry-After` is computed from the current window state, not a fixed constant — it is `max(1, X-RateLimit-Reset - now)`.

## 429 envelope

When `check_rate_limit` denies the request, Compadre returns:

```text
HTTP/1.1 429 Too Many Requests
Retry-After: 37
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1745003460
Content-Type: application/json

{
  "error": {
    "message": "Rate limit exceeded",
    "type": "rate_limit_error",
    "hint": "Too many requests. Try again in 37 seconds."
  }
}
```

Notes:

- `Retry-After` and the hint's "Try again in N seconds" are the same integer, derived from `X-RateLimit-Reset - now` (clamped to a minimum of 1). Clients can rely on either channel.
- The STT engine is not invoked on a 429. The per-replica FIFO queue is not touched.
- The access log middleware still records the request so operators can observe the 429 rate per replica.

## `/health` policy

`GET /health` has no application-layer rate limit. The endpoint is cheap, idempotent, and does not consume model compute. When called without a valid bearer it returns a minimal payload (status + voices); when called with a valid bearer it returns the authenticated detail payload (engines, queue depth, uptime, memory, version).

**Unauthenticated probe protection is the reverse proxy's job.** Caddy is expected to apply a loose IP-based rate limit on `/health` so that unauthenticated spray with shape-valid-random bearers (`cmpd_live_` + 43 valid base64url chars) cannot serialize on the single SQLite connection and starve CLI mutations. The app-layer `verify_api_key` shape check blocks trivially malformed tokens before the DB lookup, but it does not bound request rate — that is an infrastructure concern and lives in `deploy/caddy/Caddyfile`.

## Operator surface

The per-key RPM is set at issuance and mutated through the `compadre-keys` CLI:

| Intent | Command |
| --- | --- |
| Provision a key at a specific tier | `compadre-keys issue-key --customer <cust_id> --rpm <n> --billing-mode manual --price <brl>` |
| Change the cap after issuance | `compadre-keys update-key <key-prefix> --rpm <n>` |
| Retire a key | `compadre-keys revoke-key <key-prefix> --reason "..."` |

`--rpm` is validated at 1..10 000 by the CLI. Every `compadre-keys` mutation touches a sentinel file at `{KEY_REGISTRY_DB_PATH}.invalidate`; the running API stats this sentinel on each cache read and flushes its `AuthCache` the moment mtime advances. A revoked key stops authenticating on the very next request — no 60-second TTL window. `rotate-key` preserves the bucket; `revoke-key` removes the auth path, which makes the bucket irrelevant.

## Bucket persistence

Buckets live only in process memory. On container restart — whether via `docker compose restart compadre-stt`, an `update.yml` rebuild, or a crash — every key's window resets to zero. This is intentional:

- The customer's registry row (`rate_limit_rpm`) is unchanged, so policy is preserved.
- The runtime counter is disposable; losing it on restart costs at most one minute of oversized burst for active keys.
- Restart is the simplest possible cache invalidation for `AuthCache` + rate-limit combined (when you need immediate effect for a `revoke-key`, a restart also nukes any over-limit counters).

At Compadre's throughput this trade is fine. Customers rarely notice window resets, and operators already treat restart as a known-safe operation.

## Counters

Compadre exposes per-key usage counters that reset at each replica restart. Reads and updates are pure in-memory dict ops (no I/O on the request path) — the cost budget is the same ≤2 ms p99 the rate-limit dependency aims at.

| Field | Meaning |
| --- | --- |
| `requests_total` | Requests that reached the counter layer (every successful auth is counted; a later 429 does not bump this again). |
| `auth_success` | Successful authentications. Equals `requests_total` in the default config (denials raise before the counter layer). |
| `rate_limit_429` | Denials from the per-key bucket. Operators use the `rate_limit_429 / requests_total` ratio to spot "customer asking for a tier bump" vs "customer misbehaving". |
| `peak_rpm` | Max observed moving-window count since the replica started. A burst-then-idle key will still show its historical high-water mark. |
| `avg_rpm_last_60s` | Current moving-window count — same window the rate-limit enforces against. |
| `last_activity_at` | ISO 8601 UTC timestamp of the most recent `requests_total` bump. `null` before the key has served any request since start. |

Each replica writes its own JSON snapshot file at `{KEY_REGISTRY_DB_PATH}.counters.{replica_id}.json` every 15 seconds, where `{replica_id}` is `socket.gethostname()` (the container ID under Docker — stable per replica, unique across replicas sharing the same bind-mounted volume). The write is atomic: the JSON is built on the asyncio event loop, serialized, and handed to a worker thread that writes a `tempfile.mkstemp` sibling and then `os.replace`s it into place with mode `0600`. Concurrent writers (two replicas, or the shutdown cancel handler racing a mid-sleep writer) each get a unique tempfile, so no torn output is ever observable.

The snapshot is the CLI's window into the in-memory state — there is no internal HTTP endpoint. The same design rationale applies as the `.invalidate` sentinel: file-based IPC keeps the API's auth hot path small and gives the out-of-process CLI a zero-coupling read surface.

### Cache observability

`compadre-keys cache-status` globs every per-replica snapshot (`{KEY_REGISTRY_DB_PATH}.counters.*.json`) and aggregates their telemetry:

```text
Auth cache propagation bound: 60 seconds. Key-state changes become visible
to the API within this window. This command does not force invalidation;
to empty the cache faster, restart the affected replica(s).

Cache health (from snapshot written 3s ago):
  Replicas:  2
  Entries:   24 (maxsize 4096/replica)
  Hit rate:  98.5% (3684 hits / 3741 lookups)
  Uptime:    1h 01m (min across replicas)
```

The header's age label is the **freshest** replica's age — that is the timeliness of the aggregate, not an average. `Uptime` is the **minimum** across replicas — the most conservative window the data has been accumulating. Data is shown regardless of age; a missing snapshot from one replica simply drops it from the aggregate with a WARN log and the aggregate reflects the remaining replicas.

`--format json` always emits `ttl_seconds` + the verbatim propagation message, plus `replicas`, `entries`, `hits`, `misses`, `hit_rate`, `snapshot_age_seconds`, `process_uptime_seconds`, and `maxsize_per_replica` whenever at least one replica is reporting. When no replica is reporting (`replicas == 0`), the payload has `snapshot_available: false` and omits the telemetry fields; the command still exits `0` because "API is down" is a valid operational state, not a CLI error.

### Counter snapshots across replicas

Counters are aggregated across replicas by design, not approximated. Each replica writes `{db}.counters.{hostname}.json`; the CLI reads every matching file and combines them:

| Field | Aggregation |
| --- | --- |
| `requests_total`, `auth_success`, `rate_limit_429`, `avg_rpm_last_60s` | Sum across replicas (total cluster traffic). |
| `peak_rpm` | Max across replicas (highest burst observed anywhere). |
| `last_activity_at` | Max (most recent activity anywhere in the cluster). |
| `snapshot_age_seconds` | Min (age of the freshest replica reporting). |
| `process_uptime_seconds` | Min (the most conservative window the aggregate has been accumulating). |
| `entries` (cache) | Sum — each replica holds its own LRU, so this is total working-set across the cluster. |
| `hits`, `misses` | Sum — cluster-wide cache traffic. |
| `maxsize_per_replica` | Max observed (assumed uniform; reported so operators can spot misconfiguration). |

If a replica's process dies, its snapshot stops updating. Its contribution stays in the aggregate until the stale file is cleaned up (or the operator rolls the deployment — `compose.yaml` redeploys wipe old counters). This is intentional: "last-writer-wins on a single file" would silently hide N-1 replicas' traffic, which is precisely the approximation this design rejects.

### `show --audit-tail` integration

`compadre-keys show <key-prefix> --audit-tail N` appends a Last-24h stats section between the DetailPanel and the AuditTail when at least one replica's snapshot carries the resolved `key_id`:

```text
Last-24h stats (from snapshot written 3s ago, uptime 1h 01m, aggregated across 2 replicas):
  Requests total:     150
  Auth success:       150
  Rate-limit 429:     3
  Peak RPM:           35
  Avg RPM (last 60s): 8
  Last activity:      2s ago
```

The `aggregated across N replicas` note is present when `N > 1`; single-replica deployments omit it and read naturally. The section is key-target-only — `show <cust-id>` omits it entirely because the counter is per `key_id`. When no replica reports the specific `key_id`, the section degrades to a single "unavailable" breadcrumb explaining that counters reset on restart.

## Future direction

- **Shared storage for buckets.** Moving bucket state to Redis or a dedicated SQLite bucket table would make per-key caps cluster-wide-strict at the cost of one more dependency. Candidate for a future story; not shipped today.

## See also

- [monetization.md](monetization.md) — how `rate_limit_rpm` fits into customer tiers and billing.
- [configuration-reference.md](configuration-reference.md) — environment variables (no rate-limit env vars by design).
- [deployment-guide.md](deployment-guide.md) — scaling section with per-replica bucket caveat in context.
