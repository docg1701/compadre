# Implementation Plan 05 — Security Penetration Testing

## Goal

Adopt an attacker mindset (black-box, starting from the landing page at `https://<domain>` with no privileged info) and systematically probe the Compadre deployment for vulnerabilities across 8 attack surfaces. Document every finding with severity ratings and actionable remediation steps.

## Success Criteria

1. Complete attack-surface map (ports, endpoints, headers, technology stack inferred from observable behavior)
2. Auth bypass exhausted: token spraying, shape bypass, timing side-channel, cache poisoning, every denial surface probed
3. Authorization matrix verified across all key states (active / suspended / revoked / expired) and all key lifecycle transitions
4. Input validation fuzzed comprehensively: oversized, malformed, path-traversal, injection, boundary, concurrency
5. Rate-limit evasion tested: X-Forwarded-For spoofing, distributed attack, per-IP bucket bypass, multiple-header injection
6. Information disclosure surfaces audited: error messages, stack traces, `/health` over-exposure, log files, response headers
7. Caddy-level protections verified: TLS, security headers, bounded-payload policy, header injection resistance
8. Landing page tested: XSS (stored/reflected/DOM), CSRF, client-side key exposure, CSP bypass, clickjacking
9. All findings rated by severity (Critical / High / Medium / Low / Info) with a concrete fix and a verification step
10. Final report saved to `docs/plan/security-findings-05.md`

## Prerequisites

- Plan 3 complete: Compadre running on the target VPS, Caddy terminating TLS, landing page serving at `/`
- Plan 2 complete: API behavior understood — all 5 output formats, error envelope shapes, rate-limit header patterns known
- At least one active API key (`active`, `paid_until` in the future, known `rate_limit_rpm`)
- One suspended API key (for authorization testing)
- One revoked API key (for authorization testing)
- The `compadre-keys` CLI available on the VPS via `docker compose exec compadre-stt compadre-keys`
- Testing tools installed on the pentest machine (see Tool Inventory below)

### Tool Inventory

| Tool | Version | Purpose |
|------|---------|---------|
| `curl` | >= 8.0 | Manual HTTP probes, header inspection, raw TLS |
| `nmap` | >= 7.94 | Port scan, service version detection |
| `openssl s_client` | >= 3.0 | TLS version/cipher enumeration |
| `ffuf` | >= 2.1 | Directory/path fuzzing, parameter discovery |
| `python3` | >= 3.12 | Custom scripts: timing analysis, bulk spray, multipart crafting |
| `httpx` (Python) | >= 0.27 | Programmatic HTTP/2 client for scripted probes |
| Browser DevTools | Any modern | Landing page XSS/CSRF/CSP testing, network tab inspection |
| `docker compose` | >= 2.24 | Service restart, log inspection on the VPS |

---

## Severity Rating Scale

| Severity | Definition | Example |
|----------|------------|---------|
| **Critical** | Remote code execution, authentication bypass that works reliably, exposure of all API key plaintexts, or unauthenticated arbitrary file read | SQLi returning `key_hash` for arbitrary rows; RCE via ffmpeg |
| **High** | Significant information disclosure (leaked plaintext keys, full DB path), reliable DoS with one request, privilege escalation (user A accessing user B data), or auth bypass under specific conditions | Cache poisoning lets revoked key authenticate for 60s; `/health` leaks `KEY_REGISTRY_DB_PATH` |
| **Medium** | Timing side-channel that leaks key validity (enumeration), per-IP rate-limit bypass via header manipulation, CSP bypass enabling XSS on landing page, error messages that distinguish auth outcomes | Response-time oracle distinguishes "unknown key" from "suspended key"; `X-Forwarded-For: 127.0.0.1` bypasses per-IP bucket |
| **Low** | Minor information disclosure (stack trace in dev mode, version number in unexpected place), missing security header that enables low-impact attack, verbose error that hints at internal structure | `/health` unauthenticated returns model size when it shouldn't; missing `Referrer-Policy` on error pages |
| **Info** | Best-practice hardening opportunities, defense-in-depth suggestions, deviations from OWASP checklist that carry no immediate exploit | CSP uses `'unsafe-inline'` on landing page (accepted by design); rate-limit headers could include `Retry-After` on allowed requests |

### Remediation Template

For every finding, use this template in the report:

```markdown
### SEC-XXX: [Title]

- **Severity**: Critical / High / Medium / Low / Info
- **Affected Endpoint / Component**: e.g. POST /v1/audio/transcriptions, Caddyfile, src/compadre/health.py
- **Description**: What was observed — include exact curl command / script output
- **Steps to Reproduce**: Numbered list another engineer can follow
- **Impact**: What an attacker can achieve
- **Remediation**: Concrete code/config change. File path, what to change, the desired outcome.
- **Verification**: How to confirm the fix works (specific curl / test command)
- **Status**: Open / Fixed / Accepted
- **Accepted Rationale** (if Accepted): Why this is a design decision, not a bug
```

---

## Tasks

### 1. Reconnaissance — Enumerate the Attack Surface

- **Targets**: `https://<domain>` and `http://<domain>` (port 80 -> 308 redirect)
- **Actions**:

  1.1. **Port scan**

  Run `nmap -sV -p 22,80,443 --reason <target-ip>`. Verify ONLY ports 22 (SSH), 80 (HTTP->HTTPS redirect), 443 (HTTPS) are open. Any other open port is a finding.

  1.2. **HTTP -> HTTPS redirect**

  Run `curl -k -I http://<domain>/`. Verify: `308 Permanent Redirect` to `https://<domain>/`. Verify NO security headers on 308 response (Caddy's automatic-HTTPS block is exempt — documented in `test_security_headers.py`). Verify `Server` / `Via` headers are stripped even on 308.

  1.3. **Response header inventory**

  Run `curl -k -sI` on `/`, `/health`, and `/v1/audio/transcriptions`. Verify all 6 required security headers present on every response:
  - `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload`
  - `X-Content-Type-Options: nosniff`
  - `X-Frame-Options: DENY`
  - `Referrer-Policy: strict-origin-when-cross-origin`
  - `Permissions-Policy: microphone=(), camera=(), geolocation=()`
  - `Content-Security-Policy: ...` (same CSP on all non-error responses — the site-level `header { ... }` block applies to both landing page and API paths; only Caddy-generated error responses in `handle_errors` get a minimal CSP)
  - `Server` header: **must be absent** (Caddy strips it via `-Server`)
  - `Via` header: **must be absent** (Caddy strips it via `-Via`)

  1.4. **Endpoint enumeration**

  Probe known and expected-missing endpoints with `curl -k -s`. Verify `/docs`, `/openapi.json`, `/redoc` all return 404 (disabled in production via `docs_url=None, redoc_url=None, openapi_url=None`). Verify all 404 responses use a CompadreError JSON envelope. Note: app-generated 404s (from `_not_found_handler`) use `{"error": {"message", "type", "hint"}}`; Caddy-generated 404s (from `file_server` or `handle_errors`) include an additional `"status"` field: `{"error": {"message", "type", "hint", "status": 404}}`.

  1.5. **Directory brute-force**

  Run `ffuf` with a common wordlist against `https://<domain>/FUZZ`. Manually probe: `/.env`, `/.git/HEAD`, `/admin`, `/wp-admin`, `/backup`, `/config`, `/debug`, `/logs`, `/static`, `/assets`, `/api`, `/v1`, `/v2`, `/robots.txt`, `/sitemap.xml`, `/.well-known/`. Any 200 or 403 on a path not in the documented API surface is a finding.

  1.6. **OPTIONS method probe**

  Run `curl -k -s -X OPTIONS -I` on `/v1/audio/transcriptions` and `/health`. Verify `Allow` header lists only `POST` for transcription, only `GET` for health. Verify no `Access-Control-Allow-*` headers (no CORS — this is a server-to-server API).

  1.7. **HTTP method fuzzing on API paths**

  Send GET, PUT, PATCH, DELETE, HEAD, TRACE, CONNECT to `/v1/audio/transcriptions`. Verify: `GET` returns 405 with `Allow: POST`; all other methods return 405. All 405 responses use the CompadreError JSON envelope. `TRACE` must return 405 (no TRACE reflection).

  1.8. **Landing page fingerprinting**

  Open `https://<domain>/` in browser DevTools -> Network tab. Check all loaded resources. Check `localStorage`, `sessionStorage`, Cookies for any stored data. Check for `<meta>` tags that leak version or technology info.

- **Deliverable**: Attack-surface map with all exposed endpoints, response headers per endpoint, and confirmed-absent endpoints. Note any unexpected open ports, headers, or exposed paths.

---

### 2. Authentication Bypass — Token Spraying and Shape Attacks

- **Targets**: `POST /v1/audio/transcriptions`, `GET /health` (auth probe path)
- **Context from architecture**:
  - Bearer format: `cmpd_live_` + 43 URL-safe base64 chars (53 chars total)
  - Shape validation via `_VALID_TOKEN_RE` matches `\Acmpd_live_[A-Za-z0-9_-]{43}\Z`
  - Bad-shape tokens -> `ApiKeyNotFoundError` (401, envelope identical to unknown valid-shape key)
  - SHA-256 hash -> DB lookup (parameterized query)
  - 60s TTL auth cache with content-based sentinel invalidation
  - Timing equalisation: cache-hit path runs `lookup_key_auth(_DUMMY_HASH)` to match cache-miss wallclock
  - Auth shape check short-circuits before DB: blocks spray attacks from serializing on SQLite connection

- **Actions**:

  2.1. **Missing Authorization header**

  POST to `/v1/audio/transcriptions` with no Authorization header, just `response_format=text`. Expect 401 (or 422 FastAPI validation -> 400 via `validation_error_handler`). Verify response is CompadreError JSON with `type: "authentication_error"`.

  2.2. **Empty Bearer token**

  POST with `Authorization: Bearer ` (trailing space, no token). Expect 401. Message: `"Invalid or missing API key"`.

  2.3. **Wrong scheme**

  POST with `Authorization: Basic YWxhZGRpbjpvcGVuc2VzYW1l`. Expect 401. Error type must be `authentication_error`, not `invalid_request_error`.

  2.4. **Shape-invalid token — too short**

  POST with `Authorization: Bearer cmpd_live_short` (only 5 suffix chars, not 43). Expect 401. Message: `"Invalid or missing API key"`.

  2.5. **Shape-valid prefix, invalid suffix charset**

  POST with 43 exclamation marks after `cmpd_live_`. Expect 401 (shape regex only allows `[A-Za-z0-9_-]`). Must not produce 500 or different error shape.

  2.6. **NUL byte in token**

  Use Python's `urllib` to send a token containing `\x00` after `cmpd_live_`. Expect 401 or 400. Must not produce 500 or crash with traceback in response.

  2.7. **SQL injection payload in token**

  POST with `Authorization: Bearer cmpd_live_' OR '1'='1' --AAA...`. Expect 401 (shape regex rejects single quotes — not in URL-safe base64 charset). Verify NO 500, NO different error shape vs unknown key.

  2.8. **Token enumeration resistance via timing**

  Write a Python script using `httpx` that sends a valid-shaped-but-unknown token 100 times and a bad-shaped token 100 times. Measure and compare response time distributions. The mean difference should be < 50ms with statistical significance (Welch's t-test, p < 0.01). Flag if the shape-valid path (DB lookup) is measurably slower than the shape-invalid path (regex short-circuit). Note: the regex short-circuit is a documented design decision — the `cmpd_live_` prefix is public information (documented in `issue-key` output), and the timing distinction between "bad shape" and "good shape, unknown" leaks nothing an attacker couldn't already read from the docs.

  2.9. **Cache-residency oracle via timing**

  - Hit a known-valid key once (cold cache -> miss -> populate cache).
  - Hit it again within 60s (warm cache).
  - Hit an unknown key.
  - Compare response times of warm-cache hit vs cold-cache miss vs unknown key.
  - Due to F18 timing equalisation (dummy hash lookup on cache hit), the cache-hit and cache-miss paths are equalised. However, on cache-miss there are two sub-paths (known key vs unknown key) that are NOT equalised against each other — the known-key path does a DB row fetch while the unknown-key path runs `hmac.compare_digest()`. Verify cache-hit vs cache-miss are indistinguishable; flag if known-key vs unknown-key on miss show a measurable difference.

  2.10. **Cache poisoning: revoke -> verify within TTL**

  - Issue a fresh key via `compadre-keys issue-key`.
  - Make one authenticated request to populate the cache.
  - Revoke the key via `compadre-keys revoke-key <key-id> --reason "pentest"`.
  - Immediately make an authenticated request with the revoked plaintext.
  - Expect: 401 (cache invalidation sentinel should flush cache within one read cycle). If 200 returned, this is a **High** finding (revoked key authenticatable for up to 60s TTL).

  2.11. **Token in query string**

  POST with `?access_token=cmpd_live_...` in the URL, no Authorization header. Expect 401. Token in URL is logged by access-log middleware and would leak in access logs. Verify the auth dependency reads only `Authorization` header, not query params.

  2.12. **Multiple Authorization headers**

  POST with two `Authorization` headers: first invalid, second valid. Document whether FastAPI's `Header` default takes the first or raises 400. If the first (invalid) header wins -> 401. This is acceptable but should be documented.

- **Deliverable**: Auth bypass findings with timing analysis data. Every denial surface confirmed to produce identical 401 envelopes. No enumeration oracle identified.

---

### 3. Authorization — Horizontal Escalation and Key Lifecycle

- **Targets**: `POST /v1/audio/transcriptions` with keys in various states
- **Context from architecture**:
  - Key states: `active`, `suspended`, `revoked`
  - Billing check gates on `paid_until < today` (strict `<`, not `<=`)
  - `complimentary` billing mode always passes
  - Suspended key -> `ApiKeySuspendedError` (401, "Your API key has been suspended")
  - Revoked key -> `ApiKeyRevokedError` (401, "Your API key has been revoked")
  - Both carry `operator_contact_hint` in the hint field
  - Rotated key: new plaintext, same `key_id`, same rate-limit bucket

- **Actions**:

  3.1. **Active key -> 200 success**

  POST with a valid active key and a small audio file. Expect 200 with transcription output.

  3.2. **Suspended key -> 401 with suspension message**

  Suspend a key via CLI, then authenticate with it. Expect 401. Verify `error.type` is `authentication_error`. Verify message is `"Your API key has been suspended"`. Verify `hint` carries the `operator_contact_hint` (if configured).

  3.3. **Revoked key -> 401, enumeration resistance check**

  Revoke a key, authenticate with it. Expect 401. Message: `"Your API key has been revoked"`. Compare the full JSON response body with the 401 from an unknown key. The `message` field differs (by design, for operator UX) but the `error.type` is identical (`authentication_error`). Document whether this leaks enumeration information — clients can distinguish "revoked" from "never existed" by the `message` string.

  3.4. **Expired key (paid_until < today) -> 401 with suspension message**

  Set a key's `paid_until` to yesterday via `compadre-keys mark-paid --until`. Authenticate -> 401. Verify message is `"Your API key has been suspended"` (billing suspension and manual suspension produce the same envelope).

  3.5. **Complimentary key — always passes billing check**

  Issue a key with billing mode `complimentary`, no `paid_until`. Authenticate -> 200. Verify the complimentary path skips the billing gate entirely.

  3.6. **Key rotation: old plaintext dead, new alive, bucket stable**

  - Issue a key at `rate_limit_rpm=2`.
  - Make 2 requests -> both 200.
  - Rotate the key via CLI. Old plaintext -> 401. New plaintext -> 200 on first request.
  - Make 1 more request with new plaintext -> 429 (bucket persisted, 3rd request in window). Verify `X-RateLimit-Limit: 2`.

  3.7. **Two keys, same customer — independent buckets**

  - Issue two keys for the same customer, both at `rate_limit_rpm=2`.
  - Exhaust key A's budget (2 requests -> 200, 3rd -> 429). Key B: still 200.
  - Revoke key A -> key A: 401; key B: still 200.

  3.8. **`/health` authenticated: suspended key fails auth probe but gets minimal envelope**

  - Suspend a key, then call `/health` with that key's Bearer token.
  - Expect 200 with minimal envelope (status, models). Verify `memory_mb`, `uptime_seconds`, `version` are NOT in the response (auth probe failed -> falls back to minimal envelope).

- **Deliverable**: Authorization matrix showing behavior of every key state × every endpoint. No horizontal escalation. Rate-limit bucket stability across rotations confirmed.

---

### 4. Input Validation — Fuzzing the API

- **Targets**: `POST /v1/audio/transcriptions` form fields and request body
- **Context from architecture**:
  - `file` field: 25 MB max, 10 min duration max
  - ffmpeg decode -> 16 kHz mono PCM s16le WAV (subprocess, 60s timeout)
  - Caddy bounded-payload policy on API paths: 1 MiB max Content-Length, rejects chunked TE, malformed CL
  - `response_format` validated against `ResponseFormat` StrEnum: `text`, `json`, `verbose_json`, `srt`, `vtt`
  - `model`, `temperature` accepted but ignored (OpenAI SDK compat)
  - `language` passed through to Whisper engine

- **Actions**:

  4.1. **Missing `file` field**

  POST with only `response_format=text`. Expect 400 (FastAPI validation -> `validation_error_handler`, CompadreError JSON envelope). Verify `error.type` is `invalid_request_error`.

  4.2. **Zero-byte file**

  Upload `/dev/null` (or `touch /tmp/empty.opus`). Expect 400. ffmpeg fails on empty file -> `InvalidRequestError`. Verify NO 500 crash.

  4.3. **Valid audio file — baseline**

  Upload a known-good small OPUS/WAV file (e.g., the project's `audio-de-teste-01.ogg`). Expect 200 with transcription.

  4.4. **26 MB file -> oversize rejection at application layer**

  Create a 26 MB file with `dd if=/dev/urandom of=/tmp/oversize.opus bs=1M count=26`. POST it. Expect 400 with `InvalidRequestError` and message about "File too large". Verify the 25 MB limit is enforced before ffmpeg processing.

  4.5. **Content-Length > 1 MiB -> Caddy 413**

  Send a POST with `Content-Length: 1048577` (1 MiB + 1). Expect 413 from Caddy with CompadreError JSON envelope: `{"error": {"message": "Payload too large", "type": "invalid_request_error", ...}}`.

  4.6. **Chunked Transfer-Encoding -> Caddy 411**

  Send a POST with `Transfer-Encoding: chunked`. Expect 411 from Caddy with CompadreError JSON envelope.

  4.7. **Missing Content-Length on POST -> Caddy 411**

  Send a POST with neither Content-Length nor Transfer-Encoding. Expect 411.

  4.8. **Malformed Content-Length -> Caddy 400**

  Send POST with `Content-Length: abc` or `Content-Length: -1` or `Content-Length: 99999999999` (11 digits). Expect 400 from Caddy with CompadreError JSON envelope.

  4.9. **Path traversal filename**

  Upload a file with filename `../../../etc/passwd`. Verify no filesystem access. The code uses `Path(file.filename or "audio").suffix` only for the suffix; the temp file is created via `NamedTemporaryFile` which generates a unique path. This should be safe, but verify.

  4.10. **Filename with NUL byte**

  Send a multipart upload with a filename containing `\x00`. Verify graceful rejection (400, not 500 crash).

  4.11. **Extremely long filename (10 KB)**

  Send a multipart upload with a 10,000-character filename. Verify no buffer overflow or crash. The suffix extraction (`Path(file.filename or "audio").suffix`) uses only the suffix, which is typically < 10 chars.

  4.12. **Invalid `response_format` value**

  POST with `response_format=hacked`. Expect 400 with `InvalidRequestError`. Verify `hint` lists valid values: "Supported: text, json, verbose_json, srt, vtt."

  4.13. **`language` field fuzzing**

  Test: very long strings (10KB), special chars (`../../../etc/passwd`), SQL injection payloads, script tags. The value is passed directly to faster-whisper's `language` parameter. Verify no crash or unexpected behavior.

  4.14. **`model` field fuzzing**

  Test: path traversal (`../../etc/passwd`), SQL injection, script tags, very long string. The `model` field is accepted but **ignored** (MVP uses `large-v3-turbo` only). Verify the ignored value doesn't cause errors or change behavior.

  4.15. **`temperature` field fuzzing**

  Test: negative values, very large values (1e308), string values, missing. The field is accepted but **ignored**. Verify type validation catches non-float values with 400.

  4.16. **Malformed multipart body**

  Send raw JSON (`{"file": "..."}`) to the multipart endpoint with `Content-Type: application/json`. Expect 400 from FastAPI validation.

  4.17. **Concurrent oversized uploads — OOM resilience**

  Send 5 concurrent 25 MB uploads. Monitor container memory via `docker stats`. Verify no OOM kill. The lock serializes engine access and temp files are cleaned up.

  4.18. **ffmpeg command injection via filename**

  The filename is used only as `suffix` on `NamedTemporaryFile`. The file itself is passed to ffmpeg via `str(input_path)`. Verify no command injection possible through crafted filenames (ffmpeg's `-i` argument is passed as a list element, not shell-interpolated).

- **Deliverable**: Input validation finding list. Every fuzzed field documented with its behavior. Any crash, 500 error, or unexpected 200 is a Medium-or-higher finding.

---

### 5. Rate-Limit Evasion

- **Targets**: Per-key and per-IP rate limiters
- **Context from architecture**:
  - Per-key: `key:<key_id>` bucket, RPM from registry row, `MovingWindowRateLimiter`
  - Per-IP `/health`: 60/min/IP (F14)
  - Per-IP `/v1/audio/transcriptions`: 600/min/IP (F20, pre-auth)
  - All in-memory per replica — multiplying replicas multiplies effective RPM (documented)
  - `get_client_ip()` reads leftmost `X-Forwarded-For` entry, falls back to `request.client.host`
  - Caddy injects `X-Forwarded-For` on every reverse-proxy hop; `trusted_proxies private_ranges`

- **Actions**:

  5.1. **Basic per-key rate limit**

  - Issue a key at `rate_limit_rpm=3`. Send 3 requests -> all 200. Send 4th -> 429.
  - Verify 429 response: `error.type: "rate_limit_error"`, `Retry-After` header (1-61s), `X-RateLimit-Limit: 3`, `X-RateLimit-Remaining: 0`, `X-RateLimit-Reset` (future epoch).

  5.2. **`X-Forwarded-For` spoofing**

  Send two requests from the same IP but with different `X-Forwarded-For` values. The per-IP bucket uses `get_client_ip()` which reads the leftmost entry. Since Caddy is the sole proxy and always injects its own `X-Forwarded-For`, a client injecting an additional header results in two `X-Forwarded-For` headers. Check which value `get_client_ip()` uses. Per RFC 7239, Caddy **prepends** its client IP to `X-Forwarded-For` (or overwrites it for untrusted direct clients); it does not append. Verify the spoof attempt fails.

  5.3. **`X-Forwarded-For: 127.0.0.1` — trusted proxy check**

  Send with `X-Forwarded-For: 127.0.0.1`. Caddy's `trusted_proxies private_ranges` should strip/replace internal IPs. Verify the client IP is NOT `127.0.0.1` after Caddy processing.

  5.4. **Multiple `X-Forwarded-For` headers**

  Send two `X-Forwarded-For` headers: `X-Forwarded-For: 1.2.3.4` and `X-Forwarded-For: 5.6.7.8`. Check behavior of `get_client_ip()` — it takes `forwarded.split(",")[0].strip()`. Note: per the ASGI spec, duplicate headers are pre-combined with `, ` before reaching Starlette, so `request.headers.get()` returns a single comma-joined string. Verify the IP used is correct (Caddy's prepended entry).

  5.5. **`X-Real-IP` header — ignored**

  Send `X-Real-IP: 1.2.3.4`. Verify this header has NO effect on rate limiting. `get_client_ip()` only reads `X-Forwarded-For`.

  5.6. **Per-IP `/health` limit: 70 requests/min**

  Send 70 requests to `/health` from a single IP in 1 minute. The 61st request should return 429. Verify `Retry-After` and error envelope.

  5.7. **Per-IP transcription limit: 650 requests/min**

  Send 650 requests to `/v1/audio/transcriptions` from a single IP (without a valid API key) in 1 minute. Even without auth, the pre-auth per-IP check should trip at 601. Expect 429. This verifies F20: per-IP gate runs BEFORE `verify_api_key`.

  5.8. **Distributed attack: different IPs, same key**

  Use multiple machines/VPNs to send requests with the same API key. Each IP has its own per-IP bucket (600/min), but the per-key bucket (`key:<key_id>`) is global. The per-key bucket should catch the aggregate. Verify 429 at the per-key limit, not the per-IP limit.

  5.9. **`/health` unauthenticated spray -> 429, not 401**

  Send rapid `/health` requests without any `Authorization` header. Verify the 429 from per-IP rate limiter kicks in at 61/min. Verify the 429 response does NOT leak any information that would distinguish it from an authenticated 429.

  5.10. **Rate-limit bucket identity survives key rotation**

  Exhaust a key's bucket (3 requests at rpm=3 -> 429 on 4th). Rotate the key. The new plaintext should also hit 429 on its FIRST request (bucket identity `key:<key_id>` is stable across rotation). Verify this.

- **Deliverable**: Rate-limit bypass findings. Confirm no practical evasion of per-key or per-IP limits via header manipulation.

---

### 6. Information Disclosure

- **Targets**: All endpoints, error responses, logs
- **Context from architecture**:
  - `unhandled_error_handler` catches all `Exception` -> generic 500 with message `"An internal server error occurred"`, no stack trace
  - `CompadreError` subclasses never carry attacker-controlled strings unsanitized
  - `/health` unauthenticated returns `status` + `models` only; authenticated adds `engines`, `queue_depth`, `uptime_seconds`, `memory_mb`, `version`
  - Access log middleware logs `ip method path status duration` — no PII in access log
  - `Server` and `Via` headers stripped by Caddy
  - 404/405 error handlers reflect sanitized method + path only (capped at 200 chars, control chars stripped)
  - No `docs_url`, `redoc_url`, or `openapi_url` in production

- **Actions**:

  6.1. **Trigger 500 error — verify no stack trace**

  Attempt to trigger a 500 via: concurrent lock contention timeout, malformed audio that crashes ffmpeg, sending a request during warm-up. Every 500 response must have `message: "An internal server error occurred"`, no file paths, no tracebacks, no SQLite error messages.

  6.2. **`/health` unauthenticated — verify minimal envelope**

  `curl -k -s https://<domain>/health`. Expect `{"status": "healthy", "models": ["large-v3-turbo"]}`. Verify NO `memory_mb`, `uptime_seconds`, `version`, `engines`, `queue_depth` fields.

  6.3. **`/health` authenticated — verify full envelope**

  `curl -k -s https://<domain>/health -H "Authorization: Bearer <valid-key>"`. Expect `engines`, `queue_depth`, `uptime_seconds`, `memory_mb`, `version` fields present. Verify NO `KEY_REGISTRY_DB_PATH`, NO internal IPs, NO Fernet key material.

  6.4. **Error messages — check for internal paths**

  Trigger every error type and inspect the `message`, `type`, `hint` fields. Verify none contain:
  - File system paths (`/app/`, `/var/lib/compadre/`, `/home/`)
  - Internal IP addresses
  - Database schema details (`sqlite3.OperationalError`, table names)
  - Stack traces or module names (`compadre.api`, `compadre.stt`, `compadre.keys.auth`)
  - The `KEY_REGISTRY_ENCRYPTION_KEY` or any Fernet key material

  6.5. **Response headers — Server / Via / X-Powered-By**

  Check every endpoint response for `Server`, `Via`, `X-Powered-By`, `X-AspNet-Version`, `X-Generator`. All must be absent (Caddy strips `Server` and `Via`; FastAPI doesn't emit the others).

  6.6. **Version leakage**

  The Compadre version (`__version__`) appears in `/health` authenticated response. Is this acceptable? Document as **Info** finding or confirmed design decision. Check if version appears in any other response header or body.

  6.7. **Debug/trace query parameters**

  Try `?debug=true`, `?debug=1`, `?trace=1`, `?verbose=true` on every endpoint. Verify these are ignored (200 or normal error, no extra output, no stack traces).

  6.8. **Caddy access logs — check for sensitive data**

  On the VPS, run `docker compose logs caddy 2>&1 | tail -100`. Verify no API key plaintexts, no audio content, no PII in log lines. Caddy logs in JSON format to stdout. Verify no `Authorization` header values leaked in log output.

  6.9. **STT application logs — check for sensitive data**

  On the VPS, run `docker compose logs compadre-stt 2>&1 | tail -100`. Verify no token plaintexts, no `tax_id` values, no `KEY_REGISTRY_ENCRYPTION_KEY`. Access log format: `timestamp | level | access | <ip> <method> <path> <status> <duration>`. No bearer token, no request body.

  6.10. **Caddy `handle_errors` body reflection — safe from injection**

  Caddy's `handle_errors` block uses `{err.status_text}` in JSON bodies. The `status_text` is set by Go's `net/http` package from an enum (e.g., "Not Found", "Payload Too Large"), not from attacker input. Verify this cannot be manipulated by crafting a request that produces a custom status text.

  6.11. **404 path reflection — control-character sanitization**

  Send a request to `/v1/<script>alert(1)</script>`. The 404 response includes the reflected path. Verify that control characters (NUL, CR, LF, ANSI escapes) are stripped by `_sanitize_for_reflection()`. Verify the path is JSON-encoded so HTML tags in the path are harmless in a JSON response.

- **Deliverable**: Information disclosure findings. Any leaked internal paths, stack traces, key material, or PII is a High or Critical finding.

---

### 7. Caddy-Level Protections

- **Targets**: TLS configuration, HTTP security headers, bounded-payload policy
- **Context from architecture**:
  - Caddy v2 (alpine image) with automatic HTTPS
  - Site-level `header` block with HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy
  - `-Server` and `-Via` strip directives
  - Bounded-payload policy: 4 matchers (`@api_has_te`, `@api_malformed_cl`, `@api_oversized_body`, `@api_missing_cl`) on `/v1/*` and `/health`
  - `handle_errors` block re-applies security headers on Caddy error responses
  - `reverse_proxy` with `dynamic a` DNS resolution, `lb_policy least_conn`, `trusted_proxies private_ranges`

- **Actions**:

  7.1. **TLS version enumeration**

  ```bash
  openssl s_client -connect <domain>:443 -tls1_2 </dev/null 2>&1 | grep -E "Protocol|Cipher"
  openssl s_client -connect <domain>:443 -tls1_3 </dev/null 2>&1 | grep -E "Protocol|Cipher"
  openssl s_client -connect <domain>:443 -tls1_1 </dev/null 2>&1  # Should fail
  openssl s_client -connect <domain>:443 -tls1 </dev/null 2>&1    # Should fail
  ```
  Verify: only TLS 1.2 and 1.3 accepted. TLS 1.0/1.1 must be rejected.

  7.2. **Cipher suite strength**

  ```bash
  nmap --script ssl-enum-ciphers -p 443 <domain>
  ```
  Verify: no export-grade ciphers, no NULL ciphers, no RC4, no 3DES. Forward secrecy ciphers preferred.

  7.3. **Certificate validation**

  ```bash
  openssl s_client -connect <domain>:443 -servername <domain> </dev/null 2>&1 | openssl x509 -noout -dates -subject -issuer
  ```
  Verify: certificate is valid (not expired), matches the domain, issued by a trusted CA (Let's Encrypt or self-signed for `localhost`).

  7.4. **HSTS header**

  Verify `Strict-Transport-Security: max-age=63072000; includeSubDomains; preload` on every HTTPS response (excluding the 308 redirect from port 80, which is exempt per F67 documentation).

  7.5. **CSP header analysis**

  Landing page CSP: `default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; img-src 'self'; connect-src 'self'; media-src 'self' blob:; upgrade-insecure-requests; frame-ancestors 'none'; base-uri 'self'; form-action 'self'`

  Caddy-generated error response CSP (from `handle_errors`): `default-src 'none'; upgrade-insecure-requests; frame-ancestors 'none'`. Note: normal API responses (200, 400, 401, 429, etc.) receive the same full site-level CSP as the landing page; only Caddy-internal errors get the minimal CSP.

  Verify the landing page CSP permits `'unsafe-inline'` for scripts and styles (required for the inline JS+CSS in `index.html`). This is an accepted risk (no user-generated content on landing page). Document as **Info**.

  7.6. **Header injection resistance**

  Send a request with `\r\n` in a custom header value:
  ```bash
  curl -k -s https://<domain>/ -H "X-Custom: value\r\nInjected-Header: evil"
  ```
  Verify no header injection in the response. Caddy should sanitize or reject malformed headers.

  7.7. **Bounded-payload policy — all four matchers verified**

  Confirm the 4 Caddy matchers work as documented:
  - `@api_has_te`: any Transfer-Encoding on API path -> 411
  - `@api_malformed_cl`: non-canonical Content-Length -> 400
  - `@api_oversized_body`: Content-Length > 1048576 -> 413
  - `@api_missing_cl`: POST/PUT/PATCH without CL or TE -> 411

  Every response must carry the CompadreError JSON envelope AND security headers (F67 + F70).

  7.8. **Non-API paths not affected by bounded-payload policy**

  Send a 2 MB POST to `/` (landing page). Expect 405 (Method Not Allowed) or other non-413 status. Verify the 1 MiB bounded-payload policy is scoped to `/v1/*` and `/health` only.

  7.9. **Caddyfile static analysis**

  Read both `deploy/caddy/Caddyfile` and `deploy/caddy/Caddyfile-no-landing` from the repo. Verify:
  - Both declare the same four `@api_*` matchers
  - Both have the `handle_errors` block with security headers
  - All `expression` lines are identical
  - The 1 MiB threshold is `1048576` in both
  - **Critical difference**: `Caddyfile-no-landing` uses upstream name `tts-service` (should be `compadre-stt`) — this is a bug that would break the no-landing deploy
  - **Critical difference**: `Caddyfile-no-landing` `@api_*` matchers do NOT include `path /v1/* /health`, so bounded-payload policy applies site-wide (including root `/`)
  - **Critical difference**: `Caddyfile-no-landing` `@api_*` handlers emit plain-text responses (not JSON), and `handle_errors` uses `"type":"caddy_error"` (not app taxonomy)

  7.10. **HTTP/2 support**

  ```bash
  curl -k --http2 -I https://<domain>/
  ```
  Verify HTTP/2 is supported (Caddy enables it by default).

- **Deliverable**: TLS/HTTP protection findings. Any missing security header, weak cipher, or bounded-payload bypass is a finding.

---

### 8. Landing Page (Client-Side Security)

- **Targets**: `https://<domain>/` loaded in a browser
- **Context from architecture**:
  - Single-page HTML with inline CSS and JS
  - File upload via drag-and-drop + `<input type="file">`
  - API key entered in a password field, sent as `Authorization: Bearer` header
  - Bring-your-own-key model — nothing stored in browser storage
  - CSP permits `'unsafe-inline'` scripts/styles (required for inline code)
  - Form posts to same origin (`form-action 'self'`)
  - `X-Frame-Options: DENY` prevents clickjacking
  - Served by Caddy's `file_server` from `/srv/www` (read-only bind mount)

- **Actions**:

  8.1. **XSS — API key field**

  Open the page, paste `<script>alert(1)</script>` into the API key field. Submit the form. Verify NO script execution. The field is `type="password"` and the value is read via `apiKeyEl.value.trim()` — the value is set as an HTTP header, not rendered into the DOM.

  8.2. **XSS — filename reflection**

  The `fileNameEl.textContent` is set to `file.name` when a file is selected. `textContent` is safe (no HTML parsing). Verify that a filename like `<img src=x onerror=alert(1)>.opus` is displayed as text, not rendered as HTML.

  8.3. **XSS — transcription result reflection**

  The transcription result is set via `transcriptionText.textContent = displayText`. `textContent` is safe — no XSS vector. However, verify that if the API returned HTML (`<script>...</script>`), it would NOT execute.

  8.4. **XSS — error message reflection**

  Error messages from the API are rendered via `errorMessage.textContent` and `errorHint.textContent`. Both are safe. Verify no `innerHTML` usage in the error path.

  8.5. **CSRF — cross-origin form submission**

  Can a third-party website trigger a transcription by submitting a form to `https://<domain>/v1/audio/transcriptions`? The API requires `Authorization: Bearer` header which cannot be set by a cross-origin HTML form. Additionally, CORS is not configured (no `Access-Control-Allow-Origin`), so cross-origin XHR/fetch would fail the preflight. Verify this.

  8.6. **CSRF — landing page form action**

  The form on the landing page submits via JavaScript `fetch()` to `/v1/audio/transcriptions`. The form element itself has no `action` attribute and the submit event is intercepted. Verify there's no unintended CSRF vector.

  8.7. **API key client-side storage**

  Open DevTools -> Application -> Local Storage / Session Storage / Cookies for `https://<domain>`. Verify the API key is NOT stored anywhere. After submitting, refresh the page — the key field should be empty.

  8.8. **API key in URL / referrer**

  Submit a transcription. Check the Network tab — verify the `Authorization` header is present and the key is NOT in the URL query string. Check the `Referer` header in the request — it should be `https://<domain>/` without the key.

  8.9. **Clickjacking protection**

  Verify `X-Frame-Options: DENY` on the landing page. Verify `Content-Security-Policy: frame-ancestors 'none'`. Attempt to embed the page in an iframe (create a simple HTML page with `<iframe src="https://<domain>/">`) — it should be blocked.

  8.10. **Mixed content**

  If the deployment uses real certificates (Let's Encrypt), load the page over HTTPS and verify all resources (logo, fonts) are loaded over HTTPS. The Google Fonts links use `https://` explicitly. Verify no mixed-content warnings in DevTools console.

  8.11. **CSP `'unsafe-inline'` — risk assessment**

  The landing page CSP permits `script-src 'self' 'unsafe-inline'` and `style-src 'self' 'unsafe-inline'`. This is required because the JS and CSS are inlined in `index.html`. If an attacker can inject content into `index.html` (via a compromised bind mount or CI pipeline), they get script execution. This is an accepted risk given:
  - The landing page has no user-generated content
  - The HTML file is read-only bind-mounted from the host
  - An attacker who can modify the bind mount already has host access

  Document as **Info** finding with the accepted rationale.

  8.12. **Sensitive data in page source**

  View page source (`Ctrl+U`). Verify no API keys, internal URLs, or configuration details are hardcoded. The page should contain only the UI structure, inline CSS, and the transcription submission JavaScript.

  8.13. **Autocomplete on API key field**

  The API key input has `autocomplete="off"`. Verify the browser doesn't offer to save the key (password managers may still prompt — this is browser-dependent and acceptable).

- **Deliverable**: Landing page security findings. Any XSS, CSRF, or key-exposure vector is a High finding.

---

### 9. Classify, Prioritize, and Document All Findings

- **File**: `docs/plan/security-findings-05.md` (new)
- **Format**: Each finding uses the Remediation Template from the Severity Rating Scale section above. Assign IDs sequentially: `SEC-001`, `SEC-002`, etc.
- **Prioritization**:
  - **Critical**: fix immediately, re-test before proceeding
  - **High**: fix before next production deploy
  - **Medium**: fix within 30 days
  - **Low**: fix when convenient, document in backlog
  - **Info**: acknowledge and document rationale
- **Acceptance**: Report is comprehensive. Every attack vector tested. Every finding has:
  - Clear steps to reproduce
  - Concrete remediation (file path, code change)
  - Verification command to confirm the fix

---

## Files to Modify (If Vulnerabilities Are Confirmed)

These files are the expected remediation targets. Actual modifications depend on findings.

| Finding Class | Likely File | What May Need Changing |
|---------------|-------------|----------------------|
| Error message sanitization | `src/compadre/exceptions.py` | Remove internal detail from `hint` or `message` |
| Error handler hardening | `src/compadre/main.py` | Ensure `unhandled_error_handler` never leaks stack traces |
| Input validation gaps | `src/compadre/api.py` | Add missing validation for `language`, `model`, or filename |
| Rate-limit IP handling | `src/compadre/rate_limit.py` | Harden `get_client_ip()` against header injection |
| Auth timing equalisation | `src/compadre/keys/auth.py` | If timing oracle found, tighten dummy-compare placement |
| CSP hardening | `deploy/caddy/Caddyfile` and `Caddyfile-no-landing` | Adjust CSP directives |
| Landing page XSS | `deploy/landing/index.html` | Escape/sanitize user-controlled values |
| Caddy security headers | `deploy/caddy/Caddyfile` | Add missing headers or fix header ordering |

## New Files

- `docs/plan/security-findings-05.md` — complete findings report with severity ratings and remediation steps

## Dependencies

- Plan 3 (Compadre deployed on VPS) — **required**
- Plan 2 (API behavior known) — **required** (knowing expected 401/429 shapes avoids false positives)

## Risks

| Risk | Mitigation |
|------|-----------|
| Active testing could crash the service (OOM, segfault) | Run during a maintenance window; have `docker compose restart` ready on the VPS; monitor `docker stats` during concurrent tests |
| Rate-limit testing may exhaust test key's budget and block legitimate testing | Create a dedicated test key at `--rpm 10000`; rotate if exhausted; document the key ID so it can be restored |
| Timing attack testing requires statistical rigor for valid conclusions | Run at least 100 samples per test; use a Python script that records raw timestamps; compute mean, stdev, and Welch's t-test p-value; flag only p < 0.01 with mean diff > 50ms |
| Some findings may be design decisions, not bugs (e.g., `'unsafe-inline'` in CSP, `message` difference between revoked/unknown) | Document as "Accepted" with rationale referencing the architecture doc or AGENTS.md invariant |
| Production data exposure during testing | Use disposable test customers and keys only; never run against real customer data; prefix all test entities with `pentest-` |
| Off-hours testing may not be possible | Schedule with operator; alternatively run against a staging clone (if available) or limit intrusive tests |

## Validation Checklist

- [ ] Attack surface map complete (endpoints, headers, ports, technology fingerprint)
- [ ] Auth bypass attempts exhausted — no successful bypass found
- [ ] Timing analysis completed — no statistically significant oracle identified (p < 0.01, mean diff > 50ms)
- [ ] Cache poisoning test: revoked key returns 401 within cache TTL
- [ ] Authorization matrix verified — no horizontal escalation
- [ ] Key lifecycle transitions tested: issue -> use -> rotate -> old-dead/new-alive -> revoke -> dead
- [ ] Input fuzzing completed — every form field tested with boundary, injection, and malformed payloads
- [ ] No 500 errors observed from crafted inputs (all rejections are 400/401/413/422)
- [ ] Rate-limit evasion tested — X-Forwarded-For spoofing fails; distributed attack caught by per-key bucket
- [ ] Information disclosure audited — no stack traces, no internal paths, no key material in responses or logs
- [ ] Caddy TLS/HTTP protections verified — TLS 1.2/1.3 only, all 6 security headers present, bounded-payload policy enforced
- [ ] Landing page XSS/CSRF tested — no script execution from reflected values, no cross-origin form posting
- [ ] Landing page CSP analyzed — `'unsafe-inline'` risk documented as accepted
- [ ] All findings classified by severity (Critical / High / Medium / Low / Info)
- [ ] Every finding has Steps to Reproduce, Impact, Remediation, and Verification
- [ ] `docs/plan/security-findings-05.md` written and complete
- [ ] Critical and High findings fixed and re-tested (if any found)
