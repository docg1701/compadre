# Changelog

All notable changes to Compadre are tracked here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] — 2026-04-28

### Changed

- Bump version to 1.2.0 (pyproject.toml, __init__.py, README badge)

### Fixed

- **docs/deployment-guide.md**: corrected health endpoint output (unauthenticated
  callers see `{"status":"healthy"}`, not full model details). Fixed
  `health.yml` "Requires sudo" column. Added `deploy-models.yml` + restart to
  deployment flow. Added `ansible_become_password` notes for Debian UFW checks.

- **AGENTS.md**: added LLM-oriented Ansible runbook section with mandatory
  invocation pattern and friction points (become password, restart after model
  download, `-m shell` ad-hoc syntax, `--cpf`/`--cnpj` flags). References
  `docs/deployment-guide.md` to avoid redundancy.

## [2.0.0] — 2026-04-26

Multi-tenant key registry, automated billing lifecycle, LGPD-compliant
customer data handling, multi-replica scaling, and a hardening pass that
closed 23 acceptance defects across 5 review rounds.

### Added

- **Customer + API-key registry** (Epics 1–5). SQLite-backed `compadre-keys`
  CLI replaces the single `API_KEY` env var. 21 verbs covering customer
  CRUD, key issue/rotate/revoke, billing lifecycle (mark-paid / suspend /
  reactivate / suspend-sweep / at-risk / revenue), LGPD export/delete,
  encryption-key rotation, and operational health checks.
- **At-rest CPF/CNPJ encryption** via Fernet with deterministic HMAC-SHA256
  lookup digest (Story 4.1). Schema v3 partial UNIQUE INDEX prevents
  duplicate registrations across formatted vs. unformatted tax IDs while
  keeping ciphertext non-comparable.
- **Per-key rate limiting** via the `limits` library
  (`MovingWindowRateLimiter`). Keyed on the registry `key_id`, so two keys
  for the same customer bucket independently and rotation preserves the
  bucket. `/v1/audio/speech` 429 envelope ships `Retry-After` plus
  `X-RateLimit-*` headers; `/health` is unrate-limited.
- **Per-key counters** (Story 2.3) — `requests_total`, `auth_success`,
  `rate_limit_429`, `peak_rpm`, `avg_rpm_last_60s`. Snapshotted every 15s
  to per-replica JSON files; `compadre-keys cache-status` and
  `compadre-keys show <prefix>` aggregate across replicas (sum / max / min
  per metric semantics).
- **Multi-replica horizontal scaling** via `compose.yaml` `deploy.replicas`.
  Caddy `dynamic a` upstream resolver re-resolves Docker DNS every 5s and
  uses `least_conn` to exploit the single-FIFO-lock-per-replica design
  (queue-depth-aware load balancing).
- **Caddy bounded-payload policy** for `@api` (`/v1/*` + `/health`):
  rejects oversized bodies (>1 MiB), missing Content-Length on writes,
  Transfer-Encoding, and malformed Content-Length — all with the canonical
  CompadreError JSON envelope and full security-header overlay.
- **Caddy error-envelope rewrite** (`handle_errors`) so 404 / 405 /
  4xx-other / 5xx responses ship the same `{"error": {message, type,
  hint}}` shape as upstream, with security headers and `Server`/`Via`
  stripped.
- **Encrypted nightly backup** of the key registry. `backup-keys.yml`
  produces `registry.db.backup-<ISO8601>.tar.gz.enc` (AES-256-GCM,
  PBKDF2-SHA256, 600k iters); `restore-keys.yml` round-trip restores onto
  a live deployment with key-divergence detection. Daily cron at 02:00
  operator-TZ; 14-day retention.
- **Operational assertion playbook** `health.yml` (Story 4.4) — exits
  non-zero on DB mode drift, missing crons, UFW misconfig, `/health`
  unhealthy, or any `compadre-keys health` warn/fail. Wireable into
  monitoring.
- **CI non-regression suites** for security headers, docs-endpoints-
  disabled, timing-safe key comparison, oversized-body rejection, rate-
  limit wiring, no-raw-HTTPException scan, SQL-injection static analysis
  (bandit B608) — Stories 4.5a–4.5g.
- **Field-specific validation messages** for `/v1/audio/speech` body —
  pydantic taxonomy (string_too_short / string_too_long / missing /
  enum / numeric bounds / dict_type / json_invalid) all surface
  field-tagged messages instead of the legacy generic envelope.
- **Public landing page** at `/` with interactive form (API key + voice
  selector + text + speed + format + post-processing toggle), audio
  player, and download. Loads voices via `/health`. Bind-mounted in
  Caddy; gated by `landing_enabled` ansible variable.
- **Voice management playbooks** `deploy-voices.yml` (Kokoro `.npy` +
  Qwen3 `.wav` reference samples) and `deploy-models.yml` (Qwen3 GGUF
  for air-gapped deploys). Voice cloning workflow: customer sends `.wav`,
  operator runs the playbook, voice becomes available immediately.
- **New custom voices** (Kokoro): `pm_galvani`, `pm_deyvin`,
  `pf_influencer_01`, `pf_influencer_02`, `pm_influencer_01`,
  `pm_influencer_02`.

### Changed

- **Auth model retired**: legacy single-`API_KEY` env-var path removed.
  All requests authenticate against the registry; the dispatch layer is
  gone.
- **API errors** now route exclusively through the `CompadreError`
  hierarchy. Raw `FastAPI`/`HTTPException` calls are statically banned
  (CI scan in Story 4.5f).
- **Suspension sweep predicate** uses strict `paid_until < today`
  (Story 3.3). A key with `paid_until == today` is paid through today;
  the sweep catches it the next morning. Cron fires at 00:30 in the
  configured `BILLING_TIMEZONE` (default UTC; settable to operator TZ).
- **`mark-paid --months`** anchors on `paid_until` if today-or-later,
  otherwise on today (Story 5.1 Gap 3). Companion `--price-monthly` is
  atomic; audit row carries `{price_monthly_changed, old, new}`.
- **`revenue --as-of`** replays the audit log to reconstruct status,
  paid_until, and price_monthly per key on any historical date
  (Story 5.1 Gap 8). Out-of-range `--as-of` exits 2.
- **`export-customer --include-audit`** emits chronological events with
  `audit_events_total` + `audit_events_truncated` markers (Story 5.1
  review-pass), so LGPD Art. 18 II/V responses are provably complete or
  provably partial.
- **`update-customer`** distinguishes `--add-note` (append) vs `--notes`
  (replace) via `notes_op` audit discriminator. Same-value writes are
  no-ops at field granularity (no UPDATE, no audit row).

### Fixed

- 23 defects closed during the 5-round acceptance pass:
  - **B2** — partial UNIQUE INDEX on encrypted tax-ID lookup digest;
    duplicates blocked across formatted/unformatted variants and across
    Fernet rotations.
  - **B10** — manual key with `paid_until=NULL` correctly admitted as
    trial-window; expired keys correctly 401.
  - **B16** — backup cron uses bundled `compadre-keys-backup` binary +
    `/etc/compadre/backup-env`; honors retention.
  - **B19** — Caddy 4xx/5xx errors emit the project's JSON envelope.
  - **B20** — pydantic validation surfaces field-tagged messages
    (`string_*`, `missing`, `enum`, numeric bounds, dict shape,
    `json_invalid`).
  - **B21** — Caddy bounded-payload rejections (411 / 413 / 400) ship
    `application/json` envelope inline, not Caddy's text/plain default.
  - **B23** — `restore-stage.sh` magic-header detect uses portable `od`
    (the slim image lacks `xxd`).
  - **B24** — `restore-keys.yml` health probe uses bundled `python3`
    (the slim image lacks `wget`).
  - **B25** — `update-key` skips same-value writes (no audit row,
    "No changes" rendering).
  - **B26** — `deploy-models.yml` / `deploy-voices.yml` set
    `mode_parse: modern` so `"0644"` is parsed as octal (was: decimal
    644 → octal 1204).
  - Plus 13 minor exit-code, message-shape, and audit-detail fixes
    (B1 / B3–B9 / B11 / B12 / B15).

### Security

- **CPF/CNPJ encrypted at rest** with operator-rotatable Fernet key
  (`compadre-keys rotate-encryption-key`). Single decrypt site
  (`crypto.py::decrypt_tax_id`) enforced by static invariant.
- **LGPD tombstone pattern** — hard-delete preserves the audit chain
  while purging name / email / encrypted tax_id / notes (Art. 18 VI).
- **Bounded request body** (1 MiB) enforced at the Caddy layer before
  reverse_proxy runs, eliminating the silent-truncation
  `request_body { max_size }` anti-pattern.
- **Security-header overlay** (HSTS / CSP / X-Content-Type-Options /
  X-Frame-Options / Referrer-Policy / Permissions-Policy) re-applied on
  every Caddy-level error response. `Server` / `Via` stripped
  everywhere.
- **HTTPS-only** via Caddy auto Let's Encrypt or self-signed; the TTS
  service is not exposed on the host network.
- **UFW** active by default (22 / 80 / 443).
- **Encrypted backups** with AES-256-GCM + PBKDF2-SHA256 600k iters.

[2.0.0]: https://github.com/docg1701/compadre/releases/tag/v2.0.0
