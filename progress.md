# Progress — Plan 07 Review

## Review

### Correct (already good)
- **`.github/workflows/ci.yml`** is valid YAML and correctly structured — 7 parallel jobs with appropriate timeouts, dependency groups (`lint`, `test`, `type-check`, `ci`), and the `coverage` job correctly declares `needs: [test-fast]`.
- **`tests/conftest.py`** properly mocks `ensure_whisper_model` via `monkeypatch.setattr("compadre.main.ensure_whisper_model", lambda **kwargs: None)` — this patches the module-local binding so the lifespan never downloads the ~1.5 GB model.
- **`tests/test_non_regression/test_fernet_decrypt_invariant.py`** is correct — grep-scans `src/compadre/` for `fernet.decrypt`, filters `# scan-allow[single-fernet-decrypt]` comments, and asserts exactly one hit in `keys/crypto.py`. Uses `subprocess.run` with returncode 0/1 handling.
- **`tests/test_non_regression/test_no_raw_os_environ.py`** is correct — AST visitor scans for `os.environ[...]`, `os.getenv(...)`, and `from os import environ`, respects `# scan-allow[no-raw-os-environ]` comments, and exempts `config.py` and `download.py` by design. Imports `allowlisted_lines` and `safe_parse` from `_scan_common.py`.
- **`pyproject.toml`** coverage config is correct: `source = ["src/compadre"]`, `branch = true`, 60 % threshold. The `ci` dependency group contains `ansible-lint` and `yamllint`. The `dev` group correctly includes all subgroups.
- **`docs/ci-runbook.md`** is accurate and complete — mirrors the CI job table, marker contract, grep invariant, dependency groups, and troubleshooting sections. All command snippets are copy-paste runnable.
- **`AGENTS.md`** CI section matches the actual `ci.yml` — job names, commands, Docker needs, non-regression marker contract, and local reproduction commands are all consistent.
- **`scripts/ci-local.sh`** is functional — `set -euo pipefail`, pre-flight binary checks, single-command dispatch, colored output, and correct `uv` invocation patterns. All 6 subcommands (`lint`, `type-check`, `test-fast`, `ansible-lint`, `docker-build`, `coverage`) are implemented.
- **Stale TTS refs are fixed** — `grep -ri 'tts-service\|VoiceNotFoundError\|ComadreError' src/ deploy/` returns zero hits in production code. The only remaining "TTS" references are in historical plan documents (`docs/plan/`) and one intentional fixture file (`tests/test_non_regression/fixtures/no_raw_httpexception/compadre_error_only.py`) that creates a fake `VoiceNotFoundError` class to test the AST scanner.
- **All verification commands pass:**
  - `ruff check .` ✅
  - `ruff format --check .` ✅
  - `mypy src/` ✅
  - `pytest -m "not slow and not non_regression"` → 252 passed ✅
  - `pytest -m non_regression tests/test_non_regression/` → 44 passed ✅
  - `coverage report --fail-under=60` → 76 % ✅

### Fixed (issue and resolution)
- **Issue:** Two non-regression tests were failing pre-existing drift unrelated to Plan 07 itself:
  1. `test_oversized_post_is_rejected_with_413[/v1/audio/transcriptions]` expected 413 for a 2 MiB POST, but the Caddyfile was updated in an earlier story to allow up to 25 MiB on the upload path (`@api_oversized_upload`).
  2. `test_caddyfile_variants_share_the_same_body_framing_policy` expected a single threshold (`1048576`), but both Caddyfiles now declare two thresholds (`1048576` for generic API paths and `26214400` for uploads).
  **Resolution:** Updated the test module:
  - Split `_API_PATHS` into `_API_PATHS_1MIB` (`/health` only) and added `_UPLOAD_PATH` + `_25_MIB` constants.
  - Added `test_oversized_upload_is_rejected_with_413` that sends 26 MiB to the upload path and asserts 413.
  - Updated `_EXPECTED_MATCHERS` to include `@api_oversized_upload` for Caddyfile parity.
  - Updated the threshold assertion to expect `{"1048576", "26214400"}`.
  - Updated the module docstring to note the upload-path exception.

### Note (observations / follow-up)
- **`tests/test_non_regression/README.md:94`** contains the phrase "not the TTS pipeline" in the stubbing-strategy explanation. This is historical context (explaining why a stub upstream suffices) and is acceptable — it is not a stale production reference.
- **No code changes were required for Plan 07's own scope** (CI pipeline restructuring). The only edits were to the non-regression test that drifted from a prior Caddyfile change.
- **Plan 07 is complete.**

---

# Progress — Plan 05 Review

## Review

### Correct (already good)
- **Attack-surface coverage is comprehensive** across all 8 planned surfaces: reconnaissance, auth bypass, authorization, input fuzzing, rate-limit evasion, information disclosure, Caddy/TLS protections, and landing-page client-side security.
- **SEC-001 is a valid finding** — invalid `language` codes cause faster-whisper to raise `ValueError`, which the original `stt.py` caught as a generic 500 and reflected the full engine error message (including the complete list of valid language codes). This is both an information-disclosure and wrong-status-code issue.
- **Auth bypass methodology was sound** — every denial surface (missing header, empty bearer, wrong scheme, shape-invalid, NUL byte, SQLi payload, timing oracle, cache poisoning) was tested and documented. No bypass was found.
- **Rate-limit testing was sound** — XFF spoofing, multiple headers, bucket stability across rotation, and per-key / per-IP limits were all verified. No practical evasion was identified.
- **Landing-page assessment was correct** — `textContent` usage prevents XSS, no API key client-side storage, CSP `'unsafe-inline'` is an accepted design decision, and clickjacking protections are present.
- **No false positives** in the accepted/info findings (SEC-003 through SEC-008).

### Fixed (issue and resolution)
- **Issue:** `SEC-001` observed response in the report was inaccurate — it documented `type: "transcription_error"` and `hint: null`, but the actual codebase emits `type: "server_error"` with a non-null hint.
  **Resolution:** Corrected the observed response in `docs/plan/security-findings-05.md` to match the actual code behavior. The finding remains valid because the core problem (500 + leaked language list in `message`) still exists.
- **Issue:** `SEC-001` was only documented, not fixed in code. A client-controlled parameter causing a 500 with information disclosure is a real bug.
  **Resolution:** Hardened `src/compadre/stt.py::transcribe()` to catch `ValueError` from faster-whisper separately from generic `Exception`, converting it to `InvalidRequestError` (400) with a sanitized message (`"Unsupported language code"`) that does not leak the engine's internal valid-code list. Generic unexpected exceptions now emit the safe generic message `"An internal server error occurred"`.
- **Issue:** `SEC-002` severity was inflated to **Medium** in the report, but the plan's own severity scale classifies "minor information disclosure" and "missing security header that enables low-impact attack" as **Low**.
  **Resolution:** Downgraded `SEC-002` from Medium to Low in `docs/plan/security-findings-05.md` and added a severity-rationale note.
- **Issue:** `tests/test_stt.py::test_raises_compadre_error_on_transcription_failure` expected the old leaky error message (`"Transcription failed"`).
  **Resolution:** Updated the test to expect the new generic `"internal server error"` message, and added a new regression test (`test_raises_invalid_request_error_on_unsupported_language`) that asserts `ValueError` from the engine is converted to `InvalidRequestError` (400) with a sanitized hint.

### Note (observations / follow-up)
- **Caddy 1 MiB bounded-payload policy makes the app's 25 MB upload limit unreachable in production.** The report conflates Plan task 4.4 (26 MB app-level test) with task 4.5 (Caddy 1 MiB test). Any audio file larger than ~1 MiB is rejected by Caddy before it reaches the upstream, so the application-level size validator (`settings.max_upload_size_mb`) is dead code for the transcription endpoint under the current Caddy configuration. This is a functional deployment inconsistency, not a security vulnerability, but it means the service cannot accept the advertised 25 MB / 10-minute files. A follow-up task should either raise the Caddy limit to ~30 MiB or lower the app limit to match.
- **Missing explicit test documentation:** Plan tasks 4.17 (concurrent 25 MB uploads + OOM monitoring) and 5.7 (600/min per-IP transcription limit) are asserted in the validation checklist but lack raw observation notes in the report.
- **Caddyfile-no-landing "Critical differences" from Plan 7.9 were already fixed.** The three bugs described in the plan (upstream name `tts-service`, missing `path` scoping, plain-text responses / `caddy_error` taxonomy) were resolved in `plan-05-review.md` before the pentest began. The report correctly did not re-report them.
- **All 11 STT unit tests pass after the fix.** No regressions introduced.

---

# Progress — Plan 04 Review

## Review

### Correct (already good)
- **All 21 CLI verbs tested** on the live VPS (`10.10.10.208`) with 105 test cases covering the full lifecycle.
- **Exit codes 0–7 all triggered and verified** by real conditions (invalid CPF, missing customer, bad Fernet key, DB error, missing encryption key, health warn).
- **Critical invariants properly verified** by the worker:
  - Single `fernet.decrypt` call site: exactly 1 hit in `crypto.py::decrypt_tax_id` ✅
  - No raw `os.environ`: 0 hits in `src/compadre/` ✅
  - Tombstone contract: `tax_id_encrypted=X''`, `name/email/notes=""`, `tax_id_lookup=NULL`, `deleted_at IS NOT NULL`, all keys DELETEd, audit row written — all 6 checks passed via direct SQLite queries ✅
  - Encryption key rotation: re-encrypts ONLY `deleted_at IS NULL` customers (tombstones skipped), atomic rollback on `InvalidToken`, round-trip decrypt verified after `.env` update + container restart ✅
  - Sweep logic: strict `paid_until < today` (not `<=`), complimentary and NULL `paid_until` rows never touched — confirmed in `db.py::run_suspension_sweep` SQL ✅
- **Concurrent smoke tests passed**: `issue-key` and `rotate-key` during active traffic produced no DB corruption.
- **Test log accurately captures** pass/fail counts per task area and distinguishes plan-level issues from implementation bugs.

### Fixed (issue and resolution)
- **Issue:** `docs/plan/plan-04.md` Task 5.4 specified invalid CPF `"222.333.444-55"` (checksum failure) for `correct-tax-id` testing.
  **Resolution:** Changed to valid CPF `"529.982.247-25"` in plan, test script, and expected export assertion.
- **Issue:** `docs/plan/plan-04.md` Task 3.6 used `curl /health` with bearer tokens expecting 401/200 to verify key validity, but `/health` is unauthenticated (returns 200 for all callers).
  **Resolution:** Replaced curl checks with `compadre-keys show <prefix>` status verification and added a plan note explaining `/health` auth behavior.
- **Issue:** `docs/plan/plan-04.md` Task 3.2 tested `issue-key --customer nonexistent --rpm 60` expecting exit 3, but without `--price` the billing-mode validation fires first and returns exit 2.
  **Resolution:** Added `--price 149` to the test command so customer lookup executes first and returns exit 3 as intended.
- **Issue:** `docs/plan/plan-04.md` Task 4.5 expected exit 3 for `mark-paid` on a revoked complimentary key, but the CLI short-circuits on `billing_mode == "complimentary"` (exit 0, informational) before checking revocation status.
  **Resolution:** Updated plan to expect exit 0 for revoked complimentary keys and added a separate test for revoked manual keys (which correctly return exit 3).
- **Issue:** `docs/plan/plan-04-test-log.md` listed only 4 discrepancies but the summary cites 6; the missing 2 were Task 8 invariant grep false-positives (`HTTPException` handler docstrings, `console.print()` vs builtin `print()`).
  **Resolution:** Updated discrepancies section to enumerate all 6 plan-level issues with root-cause explanations.
- **Issue:** `tests/plan04_test.sh` contained the same invalid CPF, had no header explaining its hardcoded-ID dependency, and its health-exit-7 fallback could report a false pass on fresh deploys.
  **Resolution:** Fixed CPF, added prominent header comment documenting hardcoded IDs and manual-setup prerequisite, and improved the health-exit-7 fallback branch to correctly detect warn states on fresh deployments.

### Note (observations / follow-up)
- **`tests/plan04_test.sh` is a partial fragment, not a standalone reusable suite.** It covers Tasks 3–9 but omits Task 2 (customer CRUD), Task 4.10 (`revenue --as-of`), Task 6.6 (`rotate-encryption-key` round-trip), Task 8.10 (counter snapshot files), and Task 10 (cleanup). It also hardcodes IDs from a prior manual run. If this script is meant to be production-quality, it needs dynamic setup/teardown and full task coverage — likely a follow-up Plan 04b.
- **The 6 discrepancies are all plan-level, not implementation bugs.** The implementation matches `AGENTS.md` architecture invariants and exit-code contracts. No code changes were required.
- **Counter snapshot files were not created during testing** (insufficient API traffic). This is noted in the test log and is acceptable for Plan 04 scope; the counter logic is already unit-tested in `tests/`.
- **Test log shows 4 tombstones remaining in the DB** because Task 10 cleanup was not fully executed. This is harmless on a disposable test VPS but should be part of any automated suite.
- **Plan 04 is complete** pending the minor documentation/script fixes applied above.

---

# Progress — Plan 03 Review

## Review

### Correct (already good)
- `compose.yaml` — Whisper/HF env vars correctly use unprefixed names (`WHISPER_MODEL_SIZE`, `HF_HOME`, etc.) matching `config.py::Settings` (no `env_prefix`).
- `deploy/ansible/inventory/production.yml` — TTS vars fully removed, STT vars added, IP set to `10.10.10.208`, `repo_url` and `billing_timezone` configured.
- `deploy/ansible/inventory/production.yml.example` / `oracle.yml` / `oracle.yml.example` — All TTS vars removed, STT vars present with comments.
- `deploy/ansible/playbooks/bootstrap.yml` — No TTS references, no "Comadre" typos, correct STT curl example in summary, `.env` template uses unprefixed names, audio-cleanup cron removed.
- `deploy/ansible/playbooks/update.yml` — No TTS references, no "Comadre" typos, summary mentions correct volumes (`models, registry`).
- `deploy/ansible/playbooks/deploy-models.yml` — Fully rewritten for Whisper Mode B (one-shot download trigger via `docker compose run`), includes future-work Mode A comment block.
- `deploy/ansible/playbooks/health.yml` — Zero Qwen3/kokoro/voice references, zero "Comadre" typos, register var renamed to `compadre_containers`, audio volume diagnostics removed, summary uses `Model count`.
- `deploy/ansible/playbooks/cleanup.yml` — Zero "Comadre" typos, daily-restart and audio-cleanup cron removals removed, docker.list belt-and-braces fix present.
- `deploy/ansible/playbooks/backup-keys.yml` / `restore-keys.yml` — Zero "Comadre" typos, register vars renamed to `compadre_service_ps`.
- `deploy/ansible/scripts/` — `backup-keys.sh`, `restore-stage.sh`, `restore-swap.sh` comments all reference `compadre-stt`.
- `deploy/caddy/Caddyfile` — Comment correctly reads "API paths → compadre-stt".
- `deploy/caddy/Caddyfile-no-landing` — Upstream correctly set to `name compadre-stt`.
- `docs/configuration-reference.md` — Env-var table uses exact unprefixed names; zero `COMPADRE_WHISPER_` / `COMPADRE_HF_HOME` hits.
- `docs/deployment-guide.md` — Fresh-deploy runbook documented with prerequisites, secrets generation, inventory config, bootstrap command, health verification, backup/restore round-trip, rollback procedure, and idempotency notes.
- Grep invariant verified: zero hits for `kokoro`, `qwen3`, `espeak`, `tts-service`, `/v1/audio/speech` in `deploy/ansible/`, `deploy/caddy/`, and `compose.yaml`.

### Fixed (issue and resolution)
- **Issue:** `docs/deployment-guide.md:487` still referenced `COMPADRE_HF_HOME` (prefixed name never read by `config.py`).
  **Resolution:** Changed to `HF_HOME`.
- **Issue:** `tests/test_main_lifespan.py:47` set `COMPADRE_HF_HOME` in the test fixture, but `config.py` has no `env_prefix` so the variable was silently ignored.
  **Resolution:** Changed to `HF_HOME` so the test fixture matches the actual env var the app consumes.

### Note (observations / follow-up)
- **`docs/api-architecture.md`** and **`docs/api-reference.md`** still contain `COMPADRE_WHISPER_*` and `COMPADRE_HF_HOME` prefixed names (e.g. `COMPADRE_WHISPER_CPU_THREADS`, `COMPADRE_HF_HOME`). These were not in Plan 03's explicit file list and were missed by Plan 01's acceptance criteria (which focused on TTS content removal, not env-var prefix correctness). A follow-up patch should sweep these two docs for the same unprefixed names used in `configuration-reference.md`.
- **`AGENTS.md:34`** mentions `COMPADRE_WHISPER_THREADS` — also incorrect per `config.py` but outside Plan 03 scope.
- **Plan 03 completion status:** All 18 tasks from the plan are implemented in code. The inventory, compose file, all 7 playbooks, scripts, Caddyfiles, and the two specified docs are correct. Plan 03 is **complete** pending the optional follow-up doc sweep noted above.

---

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

# Progress — Plan 06 Review

## Review

### Correct (already good)
- Phased evaluation with Gold/Silver/Bronze/Fail gates is an excellent risk-management structure.
- Correctly identifies that RTF ≤ 0.3 on 4 vCPU is aggressive and provides a realistic "accept failure" exit strategy (Phase 3).
- Non-negotiable invariants (`condition_on_previous_text=False`, `word_timestamps=True`) are properly protected.
- WER regression ceiling (3 pp absolute, 2 pp for Gold) is sensible for Brazilian Portuguese.
- `stt.py` already accepts `beam_size` parameter, making greedy decoding trivial to test.
- Decision matrix format is reusable; acceptance gates are business-actionable.

### Fixed (issue and resolution)
- **Issue:** `src/compadre/api.py:158` hardcodes `beam_size=5`; the plan's file-modify list omits `api.py`.
  **Resolution:** Documented in `plan-06-review.md`; `api.py` must be added to "Files to Modify" and updated to read `settings.whisper_beam_size`.
- **Issue:** Distil-Whisper (`distil-large-v3`) — the single most promising CPU-only speedup candidate — is completely absent from the plan.
  **Resolution:** Added as Candidate 8 in review with RTF/WER estimates and drop-in replacement rationale.
- **Issue:** VAD filtering (`vad_filter=True` in faster-whisper) — a zero-integration-cost optimization that can skip 20–40 % silence in WhatsApp audio — is not mentioned.
  **Resolution:** Added as Task 1c in Phase 1; could obsolete whisper.cpp before any model change.
- **Issue:** Thread-count sweep and CTranslate2 compute_type sweep are absent; the plan assumes 4 threads / int8 is optimal without evidence.
  **Resolution:** Added Task 1b (thread sweep) and Task 1d (compute_type sweep) to Phase 1.
- **Issue:** `jiwer` is not in `pyproject.toml`; benchmark harness cannot run without it.
  **Resolution:** Flagged in review; must be added as a dev/test dependency before plan execution.
- **Issue:** `tests/fixtures/pt-br-reference/` does not exist and the plan provides no dataset acquisition strategy.
  **Resolution:** Specified Common Voice Portuguese test split or operator-recorded clips as the two viable sources.
- **Issue:** Effort estimate 3–5 days is optimistic; whisper.cpp integration + formatter adapter is realistically 3–5 days alone.
  **Resolution:** Revised estimate to 5–8 days with early-exit criteria (Distil-Whisper or greedy Silver → skip whisper.cpp).

### Note (observations / follow-up)
- **`temperature` form field in `api.py:60` is dead code** — accepted from clients but never passed to `engine.transcribe()`. Plan 06 could evaluate whether wiring `temperature=0.0` improves greedy decoding stability, or the parameter should be removed.
- **Plan 03 dependency is implicit, not explicit.** If compose.yaml env-var mismatches are not fixed, benchmarks may run with silently wrong settings. The plan should require `health.yml` to pass before benchmarking begins.
- **The decision matrix scores are pre-research heuristics.** They should be regenerated from measured numbers in `plan-06-benchmark-results.md`; the review recommends annotating them as such.
- **Greedy + `condition_on_previous_text=False` interaction risk is noted but not tested.** The combined effect on long audio (> 60 s) may be worse than the sum of individual effects; a repetition-rate stress test should be added.

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
