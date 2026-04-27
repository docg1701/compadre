# Review — Plan 05: Security Penetration Testing

**Reviewed:** 2026-04-27  
**Reviewer:** Review-and-fix subagent  
**Scope:** `docs/plan/plan-05.md` against current implementation (`src/compadre/`, `deploy/caddy/`, `deploy/landing/`, `tests/`)

---

## Executive Summary

Plan 05 is the strongest plan in the sequence so far. It demonstrates deep understanding of the architecture, correctly identifies three real bugs in `Caddyfile-no-landing`, and provides actionable test scripts for every attack surface. However, it underestimates effort, omits ffmpeg/DoS and SSRF vectors, and does not explicitly instruct the pentester to run the existing non-regression suite as a baseline. Three real bugs identified in the plan were still present in the codebase and have been fixed during this review.

**Overall rating:** Good with critical corrections applied.

---

## 1. Completeness — Attack Surface Coverage

| Surface | Covered? | Notes |
|---------|----------|-------|
| Landing page (XSS, CSRF, CSP, clickjacking) | ✅ | Section 8 — thorough for static HTML |
| API auth (bypass, spraying, timing, cache) | ✅ | Section 2 — exhaustively probes every denial path |
| API authorization (key lifecycle states) | ✅ | Section 3 — matrix covers active / suspended / revoked / expired / complimentary / rotated |
| File upload / multipart | ✅ | Section 4 — size, duration, filename, ffmpeg injection |
| Rate limiting (per-key, per-IP, evasion) | ✅ | Section 5 — XFF spoofing, distributed attack, bucket stability |
| Caddy / TLS / headers | ✅ | Section 7 — TLS 1.2/1.3, cipher suites, HSTS, CSP, bounded-payload |
| Error handling / info disclosure | ✅ | Section 6 — stack traces, internal paths, version leakage, logs |
| **LGPD / encryption boundaries** | ⚠️ Partial | Mentioned in info-disclosure (6.4, 6.9) but no dedicated section |
| **ffmpeg / audio DoS** | ❌ Missing | No malformed-audio zip-bomb or CPU-exhaustion tests |
| **SSRF / egress** | ❌ Missing | No test for coercing server into network requests |
| **Queue / lock starvation** | ❌ Missing | Single FIFO lock (`asyncio.Lock`) is not stress-tested |

**Verdict:** Covers ~90 % of the black-box surface. The missing 10 % (ffmpeg DoS, SSRF, lock starvation) are the most likely real-world attack vectors against an STT service.

---

## 2. Execution Clarity

### What is crystal clear
- Every numbered action includes the **exact curl command or Python snippet**.
- Expected status codes and response body shapes are specified verbatim.
- The Severity Rating Scale and Remediation Template are copy-paste ready.
- Prerequisites (keys in specific states, tools, CLI access) are explicit.

### What is ambiguous
- **Section 6.1 — "Trigger 500 error"** lists "concurrent lock contention timeout" as a vector but gives no recipe. The `asyncio.Lock` has no timeout, so this is impossible without a code change. The plan should either remove this or add a script that holds the lock artificially.
- **Section 2.9 — cache-residency oracle**: The procedure is correct, but the expected conclusion ("cache-hit vs cache-miss are indistinguishable") is architecture-specified, not empirically guaranteed on every VPS. The plan should note that CPU throttling or noisy neighbors can invalidate the 50 ms threshold.
- **Section 5.6 / 5.7** — per-IP limits of 70 and 650 requests: the actual limits are 60 and 600. The plan rounds up, which is sloppy. A senior developer might wonder if the limits were changed.

**Recommendation:** Add a "Pre-flight baseline" step: run `uv run pytest -m non_regression` and record results before any manual probing.

---

## 3. Risk Assessment

| Risk | Severity | Mitigation in plan? | Gap |
|------|----------|---------------------|-----|
| Service crash during concurrent oversized upload test | Medium | Yes (docker compose restart ready) | No explicit memory cap or OOM-kill monitoring script |
| Rate-limit testing blocks legitimate test key | Low | Yes (dedicated high-RPM key) | No "key reset" script provided |
| False positive in timing analysis | Medium | Yes (Welch's t-test, 100 samples) | No mention of network latency variance on VPS vs LAN |
| **ffmpeg CPU exhaustion via malicious audio** | **High** | **No** | **Missing entirely — this is the most likely DoS vector** |
| **Container escape via crafted filename → ffmpeg arg injection** | **Medium** | **Partial (4.18)** | Only tests shell injection; ffmpeg's `-i` is passed as a list element, but codec/stream options are not tested |
| Production data exposure | Low | Yes (disposable test entities) | No explicit `pentest-` prefix enforcement in CLI examples |

---

## 4. Stale TTS / Comadre References

### In the plan document itself
- **Clean.** The plan uses "Compadre" throughout. No `ComadreError`, `VoiceNotFoundError`, or `tts-service` references inside `plan-05.md` itself.

### In the actual codebase (found during review)
| Location | Stale reference | Fix applied |
|----------|----------------|-------------|
| `deploy/caddy/Caddyfile-no-landing:65` | `name tts-service` → upstream broken | ✅ Changed to `name compadre-stt` |
| `deploy/caddy/Caddyfile-no-landing` | `@api_*` matchers lacked `path` scoping; bounded-payload applied site-wide | ✅ Added `path /v1/* /health` to all matchers |
| `deploy/caddy/Caddyfile-no-landing` | `@api_*` handlers emitted plain text, not JSON | ✅ Rewrote to match main Caddyfile JSON envelopes |
| `deploy/caddy/Caddyfile-no-landing` | `handle_errors` used `"type":"caddy_error"` instead of app taxonomy | ✅ Added 404/405/4xx/5xx specific handlers with correct types |
| `deploy/caddy/Caddyfile:148` | Comment referenced `VoiceNotFoundError` (dead TTS class) | ✅ Changed to `not_found_error` |
| `deploy/caddy/Caddyfile:100` | Comment said "API paths → tts-service" | ✅ Changed to "compadre-stt" |
| `deploy/ansible/scripts/backup-keys.sh` | Comments referenced "tts-service container" | ✅ Changed to "compadre-stt" |
| `deploy/ansible/scripts/restore-stage.sh` | Comment referenced "tts-service image" | ✅ Changed to "compadre-stt" |
| `deploy/ansible/scripts/restore-swap.sh` | Comment referenced "tts-service is stopped" | ✅ Changed to "compadre-stt" |

**Note:** These bugs were already documented in Plan 03 (deployment) but were never fixed. Plan 05 correctly identifies them in Section 7.9, but treats them as "findings to discover" rather than "known bugs to verify are fixed." The plan should be updated to note that these are **pre-existing issues** and the pentest should confirm the fix.

---

## 5. Alignment with Current Implementation

### What matches perfectly
- **Multipart upload** — correct fields (`file`, `model`, `language`, `response_format`, `temperature`).
- **5 response formats** — `text`, `json`, `verbose_json`, `srt`, `vtt` are all tested.
- **25 MB / 10 min limits** — enforced in `api.py` via `settings.max_upload_size_mb` and `settings.max_audio_duration_seconds`.
- **Caddy bounded-payload policy** — 1 MiB, four matchers, JSON envelopes. Matches implementation exactly.
- **Auth cache invalidation** — F17 content-based sentinel is correctly described (Section 2.10).
- **Timing equalisation** — F18 dummy-hash SELECT on cache hit is correctly described (Section 2.8, 2.9).
- **Rate-limit buckets** — `key:<key_id>` stable across rotation, per-IP buckets on `/health` (60) and `/v1/audio/transcriptions` (600). All correct.

### What is slightly off
- **Section 4.4 — 26 MB file**: The app validates `file.size` first (FastAPI's `UploadFile.size`), then streams to temp and validates total bytes. A 26 MB file may be rejected at the `file.size` check **before** streaming. The plan says "Verify the 25 MB limit is enforced before ffmpeg processing" — this is true, but the plan should note that the rejection happens at the Starlette/FastAPI layer, not the app layer, if `file.size` is populated.
- **Section 4.18 — ffmpeg command injection**: The plan correctly states the filename is used only for `suffix` and the path is passed as a list element. However, it doesn't test whether the **file content** could be a polyglot that tricks ffmpeg into executing commands (e.g., a WAV file with embedded `ffmetadata`). This is a deeper test that should be added.

---

## 6. OWASP Coverage

| OWASP Category | Covered? | How |
|----------------|----------|-----|
| A01 — Broken Access Control | ✅ | Auth bypass (2), authorization matrix (3), horizontal escalation (3.7) |
| A02 — Cryptographic Failures | ⚠️ Partial | TLS cipher enumeration (7.1–7.3); no test for Fernet key rotation or encryption at rest |
| A03 — Injection | ✅ | SQLi in token (2.7), command injection via filename (4.18), header injection (7.6) |
| A04 — Insecure Design | ✅ | Rate-limit evasion (5), cache poisoning (2.10), timing oracles (2.8–2.9) |
| A05 — Security Misconfiguration | ✅ | Security headers (1.3, 7.4–7.5), docs disabled (1.4), method fuzzing (1.7) |
| A06 — Vulnerable Components | ❌ Missing | No test for outdated ffmpeg or faster-whisper versions |
| A07 — ID and Auth Failures | ✅ | Token spraying (2), shape bypass (2.4–2.6), cache poisoning (2.10) |
| A08 — Data Integrity Failures | ⚠️ Partial | No explicit test for tampered multipart body or checksum |
| A09 — Security Logging Failures | ✅ | Log inspection (6.8–6.9), access log format verified |
| A10 — SSRF | ❌ Missing | No SSRF tests |

**Recommendation:** Add an explicit "Vulnerable Components" task: run `ffmpeg -version` and `python -c "import faster_whisper; print(faster_whisper.__version__)"` inside the container and check against CVE databases.

---

## 7. Caddy Security Headers Contract

### Coverage in the plan
- **Section 1.3** — Header inventory on `/`, `/health`, `/v1/audio/transcriptions`
- **Section 7.4–7.5** — HSTS and CSP analysis
- **Section 7.9** — Caddyfile static analysis (excellent)

### Non-regression suite overlap
The existing `tests/test_non_regression/test_security_headers.py` already covers:
- All 6 required headers on 5 probe paths
- `Server` / `Via` banner strip
- HTTP→HTTPS 308 redirect exclusion
- Static parity assertion on both Caddyfiles

**Gap:** The plan does not tell the pentester to **run the non-regression suite first**. This is a missed opportunity to establish baseline pass/fail before manual probing. A senior developer might skip the automated tests and waste time rediscovering what code already asserts.

**Recommendation:** Add a prerequisite step: "Run `uv run pytest -m non_regression` and confirm 100 % pass before beginning manual probes."

---

## 8. LGPD / Encryption Boundaries

### What the plan covers
- **Section 6.4** — Error messages must not contain `tax_id`, `KEY_REGISTRY_ENCRYPTION_KEY`, or Fernet material.
- **Section 6.9** — Application logs must not contain token plaintexts or `tax_id` values.
- **Section 2.7** — SQLi payload in token does not leak DB schema.

### What is missing
- **No dedicated LGPD section.** The architecture invariants (Fernet encryption ONLY in `crypto.py`, audit log write-once read-via-CLI, tombstone zero-length BLOB) are not explicitly tested from the API surface.
- **No test for `tax_id_encrypted` exposure:** A pentest should verify that no API response (including error responses under DB corruption) ever includes the `tax_id_encrypted` BLOB or any derived hash.
- **No test for audit log accessibility via API:** The architecture says the audit log is CLI-only, but the plan doesn't explicitly probe for an exposed `/audit` or `/admin` endpoint.
- **No test for `compadre-keys` CLI output leakage:** If the CLI is accessible via `docker compose exec`, an attacker with container access could export customers. This is a container-level risk, not an API risk, but worth documenting.

**Recommendation:** Add Section 9 "LGPD / Encryption Boundary Verification" with tasks:
1. Grepp all API response bodies for `tax_id`, `encrypted`, `fernet`, `ciphertext`.
2. Verify `/v1/audio/transcriptions` never writes to `audit_log` (already in `test_api.py`, but should be manually confirmed).
3. Verify `compadre-keys export-customer` output is not reachable via any HTTP path.

---

## 9. Effort Estimate Accuracy

The plan estimates **6–10 hours**.

**Actual estimate for thorough execution:** **12–18 hours**.

| Task | Plan estimate | Realistic |
|------|---------------|-----------|
| Reconnaissance (1) | 1 h | 1 h |
| Auth bypass (2) | 1.5 h | 2–3 h (timing analysis alone is 1–2 h) |
| Authorization (3) | 1 h | 1 h |
| Input fuzzing (4) | 1.5 h | 2–3 h (ffmpeg crash testing adds time) |
| Rate-limit evasion (5) | 1 h | 1.5 h |
| Info disclosure (6) | 1 h | 1.5 h (log grepping across containers) |
| Caddy protections (7) | 1 h | 1.5 h (cipher suite enumeration is slow) |
| Landing page (8) | 1 h | 1 h |
| Documentation (9) | 1 h | 2 h (remediation template per finding is verbose) |
| **Total** | **6–10 h** | **12–18 h** |

**Recommendation:** Update the plan to state 10–16 hours, or split into two sessions (Day 1: automated + recon/auth/authz/rate-limit; Day 2: fuzzing + Caddy + landing page + documentation).

---

## 10. Findings Severity Summary

| ID | Finding | Severity | Status |
|----|---------|----------|--------|
| SEC-001 | `Caddyfile-no-landing` upstream name `tts-service` breaks no-landing deploy | **Critical** | **Fixed** |
| SEC-002 | `Caddyfile-no-landing` plain-text error responses break API contract | **High** | **Fixed** |
| SEC-003 | `Caddyfile-no-landing` `handle_errors` uses `caddy_error` instead of app taxonomy | **High** | **Fixed** |
| SEC-004 | `Caddyfile-no-landing` bounded-payload policy site-wide (no `path` scoping) | **Medium** | **Fixed** |
| SEC-005 | Missing ffmpeg DoS / malformed-audio test vectors | **High** | Open — add to plan |
| SEC-006 | Missing SSRF / egress test | **Medium** | Open — add to plan |
| SEC-007 | Missing lock-starvation / queue-contention DoS test | **Medium** | Open — add to plan |
| SEC-008 | Missing LGPD/encryption boundary verification section | **Medium** | Open — add to plan |
| SEC-009 | Plan does not instruct running non-regression suite as baseline | **Low** | Open — add to plan |
| SEC-010 | Effort estimate 6–10 h is optimistic | **Info** | Open — update to 12–18 h |

---

## 11. Actionable Recommendations

### Before pentest begins
1. **Run non-regression suite:** `uv run pytest -m non_regression` must pass 100 %.
2. **Fix known bugs first:** The three `Caddyfile-no-landing` issues (already fixed in this review) should be deployed before the pentest, or the pentest will waste time on pre-documented bugs.
3. **Create a `pentest-` customer and keys** via `compadre-keys` so findings don't pollute real customer data.

### Plan amendments (to `plan-05.md`)
1. **Add Section 4.19:** `ffmpeg` DoS via malformed audio (extreme sample rate, zip-bomb WAV, ffmetadata injection).
2. **Add Section 4.20:** SSRF / egress test — verify the server cannot be coerced into connecting to internal IPs via the `model` or `language` fields (negative test).
3. **Add Section 5.11:** Lock starvation — send 10 concurrent legitimate requests and verify queue depth increments/decrements correctly, no deadlock.
4. **Add Section 9:** LGPD / encryption boundary verification (see above).
5. **Update effort estimate:** 12–18 hours.
6. **Add prerequisite:** Run non-regression suite and attach results to the findings report.

### During pentest
1. Record **raw timing data** (CSV) for all timing tests, not just p-values.
2. Take **screenshots** of landing page DevTools for the report.
3. Use `docker stats` during concurrent upload tests and attach memory/CPU graphs.

---

## Validation

- [x] All non-regression tests pass after fixes: `227 passed, 42 deselected`
- [x] `Caddyfile-no-landing` static parity test passes
- [x] No stale `tts-service`, `ComadreError`, or `VoiceNotFoundError` references remain in `deploy/`
- [x] Code changes are minimal and focused on the identified bugs
