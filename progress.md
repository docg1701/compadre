# Progress — Plan 01 Review

## Review

### Correct (already good)
- The grep inventory in `plan-01.md` is exhaustive and covers every TTS→STT mutation vector.
- Task-level acceptance criteria (grep returns zero, curl examples runnable) are well-defined.
- Historical docs are correctly identified for banner preservation, not body rewriting.
- Risk table format (Likelihood × Mitigation) is clear and reusable.
- Dependency sequencing correctly places Plan 1 in Cycle 1, parallel with Plan 2.
- The validation checklist at the bottom of `plan-01.md` is a strong close-out instrument.

### Fixed (issue and resolution)
- **Issue:** `plan-01.md` describes docs as 100 % TTS stale, but in reality ~90 % were already rewritten.
  **Resolution:** Wrote `docs/plan/plan-01-review.md` documenting the actual state, recommending the plan be marked complete and retired.
- **Issue:** Per-task effort estimates sum to ~12 h, but the plan claims 4–6 h.
  **Resolution:** Documented in review with a detailed breakdown table; recommended future plans state parallelism assumptions explicitly.
- **Issue:** Plan excludes code changes from scope, but the audit found stale code refs (`exceptions.py`, `rate_limit.py`, `api.py`) that had to be fixed to make docs coherent.
  **Resolution:** Recommended adding a "Doc→Code verification" task to all future documentation plans.
- **Issue:** Missing non-markdown artifacts (`compose.yaml`, `.env.example`, `deploy/landing/index.html`, Ansible inventories).
  **Resolution:** Recommended expanding scope to "all human-readable project files that describe the system."
- **Issue:** Task 7 uses line-number citations (`deployment-guide.md:480-504`) which drift.
  **Resolution:** Recommended replacing with heading anchors or content-based search strings.
- **Issue:** No rollback strategy for complete rewrites.
  **Resolution:** Recommended adding a git-branch pre-flight note.

### Note (observations / follow-up)
- `docs/plan/audit-checklist.md` already exists and shows Plan 1 is effectively complete (all 15 docs passed, 8 code fixes applied, 17 discrepancies resolved). The master plan index at `docs/plan/README.md` should be updated to reflect this.
- `docs/checkpoint-2026-04-26.md` is the most recent checkpoint and already covers much of the Plan 1 execution; it is in Portuguese (acceptable per checkpoint convention).
- `plan-01.md` itself lacks a "Last updated" date — ironic given that Task 16 requires one for every doc.
- The only remaining grep hit for TTS terms across all current docs is the intentional parenthetical in `deployment-guide.md` line 634 ("legacy from the prior TTS project"), which is acceptable context.

---

# Progress — Plan 05 Review

## Review

### Correct (already good)
- Attack-surface map (Section 1) is exhaustive: ports, headers, endpoints, methods, technology fingerprint.
- Auth bypass (Section 2) correctly tests every denial path: missing header, empty bearer, wrong scheme, shape-invalid, NUL byte, SQLi payload, timing oracle, cache poisoning.
- Authorization matrix (Section 3) covers all key states and lifecycle transitions (issue → rotate → revoke).
- Rate-limit evasion (Section 5) tests XFF spoofing, multiple headers, distributed attack, bucket stability across rotation.
- Caddy protections (Section 7) are deeply understood: bounded-payload policy, TLS enumeration, cipher suites, header injection resistance.
- Landing page (Section 8) correctly identifies that `textContent` makes XSS impossible and CSP `'unsafe-inline'` is an accepted risk.
- Remediation Template is production-ready and will produce a high-quality `security-findings-05.md`.
- The plan correctly identifies three real bugs in `Caddyfile-no-landing` (upstream name, plain-text errors, `caddy_error` taxonomy).

### Fixed (issue and resolution)
- **Issue:** `deploy/caddy/Caddyfile-no-landing` upstream name was `tts-service` instead of `compadre-stt` — would break no-landing deploy.
  **Resolution:** Fixed upstream name to `compadre-stt` in `Caddyfile-no-landing`; also fixed stale comment in main `Caddyfile`.
- **Issue:** `deploy/caddy/Caddyfile-no-landing` `@api_*` handlers emitted plain text, breaking the API JSON contract.
  **Resolution:** Rewrote all four handlers to emit the same CompadreError-shaped JSON envelopes as the main `Caddyfile`.
- **Issue:** `deploy/caddy/Caddyfile-no-landing` `handle_errors` used `"type":"caddy_error"` instead of the app's taxonomy (`not_found_error`, `invalid_request_error`, `server_error`).
  **Resolution:** Added status-specific `@cerr_*` matchers and handlers matching the main `Caddyfile`.
- **Issue:** `deploy/caddy/Caddyfile-no-landing` bounded-payload policy lacked `path` scoping, applying site-wide.
  **Resolution:** Added `path /v1/* /health` to all four matchers and scoped `reverse_proxy` to `@api` for parity.
- **Issue:** Stale TTS references remained in `deploy/ansible/scripts/` (`backup-keys.sh`, `restore-stage.sh`, `restore-swap.sh`) and in `deploy/caddy/Caddyfile` comment (`VoiceNotFoundError`).
  **Resolution:** Replaced all stale references with STT-appropriate terminology.
- **Issue:** Plan effort estimate 6–10 h is optimistic for the depth described.
  **Resolution:** Documented in `plan-05-review.md` with detailed breakdown; realistic estimate is 12–18 h.

### Note (observations / follow-up)
- **Missing attack vectors** should be added to `plan-05.md` before execution:
  1. **ffmpeg DoS** (malformed audio / zip-bomb WAV) — the most likely real-world DoS vector against an STT service.
  2. **SSRF / egress** negative test — confirm `model`/`language` fields cannot coerce network requests.
  3. **Lock starvation** — stress-test the single FIFO `asyncio.Lock` with concurrent legitimate requests.
  4. **LGPD / encryption boundary verification** — dedicated section to confirm `tax_id_encrypted` never appears in API responses.
- **Non-regression suite** should be run as a pre-flight baseline. The plan does not mention `pytest -m non_regression`, which already asserts 24 security-critical behaviors.
- **Plan 03** already documented the `Caddyfile-no-landing` bugs but they were never fixed. This suggests a process gap: plan findings are not being tracked to closure. Recommend adding a "Findings Register" to `docs/plan/`.
- `docs/plan/plan-05-review.md` contains the full structured review with severity ratings and actionable recommendations.
