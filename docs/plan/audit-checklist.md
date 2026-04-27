# Documentation Audit Checklist

**Audit date:** 2026-04-26 (re-validated 2026-04-27)
**Auditor:** assistant
**Scope:** All documentation files in `docs/`, `README.md`, and `AGENTS.md`

---

| # | File | Status | Issues Found | Fixed | Notes |
|---|------|--------|-------------|-------|-------|
| 1 | `docs/api-architecture.md` | ✅ Passed | Complete TTS rewrite needed | Yes | Rewritten for STT: endpoint, pipeline, engine params, Docker Compose, file deps |
| 2 | `docs/api-reference.md` | ✅ Passed | Complete TTS rewrite needed | Yes | Rewritten for STT: multipart/form-data, 5 response formats, error examples |
| 3 | `docs/configuration-reference.md` | ✅ Passed | TTS env vars, Comadre branding | Yes | Removed all TTS vars, added STT vars, updated compose/.env examples |
| 4 | `docs/deployment-guide.md` | ✅ Passed | Complete TTS rewrite needed | Yes | Rewritten for STT: inventory vars, playbooks, scaling, model deploy |
| 5 | `docs/troubleshooting.md` | ✅ Passed | TTS-specific sections | Yes | Removed TTS engine issues, added STT issues (ffmpeg, upload, duration, format) |
| 6 | `docs/local-testing-guide.md` | ✅ Passed | Complete TTS rewrite needed | Yes | Rewritten for STT: no model pre-download, multipart curl examples, 5 formats |
| 7 | `docs/monetization.md` | ✅ Passed | Endpoint, paths, brand | Yes | Fixed endpoint to /v1/audio/transcriptions, src/compadre paths, Compadre brand |
| 8 | `docs/rate-limiting.md` | ✅ Passed | Endpoint, brand | Yes | Fixed endpoint to /v1/audio/transcriptions, Comadre → Compadre |
| 9 | `docs/compadre-brief.md` | ✅ Passed | Historical context | Yes | Banner added — historical reference, body preserved |
| 10 | `docs/compadre-development-plan.md` | ✅ Passed | Historical context | Yes | Banner added — historical reference, body preserved |
| 11 | `docs/proxmox-vm-setup.md` | ✅ Passed | VM name, TTS references | Yes | comadre-tts → compadre-stt, removed Qwen3 notes, added ARM support note |
| 12 | `docs/checkpoint-2026-04-26.md` | ✅ Passed | Snapshot banner | Yes | Banner added |
| 13 | `docs/claude-code-instruction-quality-guide.md` | ✅ Passed | No stale refs | Yes | Reviewed — no TTS/Comadre refs; added Last updated date |
| 14 | `README.md` | ✅ Passed | Verify STT consistency | Yes | Verified — no stale TTS refs; added Last updated date |
| 15 | `AGENTS.md` | ✅ Passed | service.py refs, file paths | Yes | Fixed service.py → stt.py, updated src/compadre/ file list, added Last updated |

---

## Validation Results

Run these grep commands after audit; expect **zero hits** on each in current docs (excludes historical documents: `compadre-brief.md`, `compadre-development-plan.md`, `checkpoint-*.md`; excludes plan documents which describe findings):

| Command | Hits | Status | Notes |
|---------|------|--------|-------|
| `grep -rni "kokoro\|qwen3\|qwen\b" docs/ README.md AGENTS.md` | 0 in current docs | ✅ | Historical docs (brief, dev-plan) preserve original text with banners |
| `grep -rni "espeak" docs/ README.md AGENTS.md` | 0 in current docs | ✅ | Historical docs preserve original text |
| `grep -rni "/v1/audio/speech" docs/ README.md AGENTS.md` | 0 in current docs | ✅ | Historical docs preserve original text |
| `grep -rni "DEFAULT_VOICE\|VOICES_DIR\|ESPEAK_DATA_PATH\|NOISE_DIR" docs/ README.md AGENTS.md` | 0 in current docs | ✅ | |
| `grep -rni "KOKORO_\|QWEN3_\|ESPEAK_" docs/ README.md AGENTS.md` | 0 in current docs | ✅ | |
| `grep -rni "src/comadre" docs/ README.md AGENTS.md` | 0 in current docs | ✅ | |
| `grep -rni "TTS engine\|TTS generation\|TTS service\|TTS request\|speech synthesis\|text-to-speech\|generate.speech\|generated.audio" docs/ README.md AGENTS.md` | 0 in current docs | ✅ | |
| `grep -rni "voice.*routing\|voice.*select\|voice.*cloning\|voice.*management\|voice.*profile" docs/ README.md AGENTS.md` | 0 in current docs | ✅ | |
| `grep -rni "comadre-stt\|comadre-keys\b" docs/ README.md AGENTS.md` | 0 in current docs | ✅ | |
| `grep -rni '"input".*:.*".*"' docs/api-reference.md docs/deployment-guide.md docs/local-testing-guide.md` | 0 | ✅ | No JSON body examples in STT docs |

---

## Code Fixes Applied (Post-Doc Review)

| # | File | Issue | Fix |
|---|------|-------|-----|
| C1 | `src/compadre/api.py` | `await file.read()` loaded entire upload into memory before size check — DoS vector for requests without `Content-Length` | Streamed upload in 64 KB chunks; total size validated during write |
| C2 | `src/compadre/api.py` | `NamedTemporaryFile(delete=False)` leaked temp file if `file.read()` raised (e.g., connection abort) | Wrapped stream in try/except that unlinks temp file on any exception |
| C3 | `src/compadre/preprocessing.py` | `get_audio_duration()` returned `0.0` on ffprobe failure, bypassing the 10-minute duration limit for corrupted files | Return `None` on failure; caller in `api.py` treats `None` as a 400 error |
| C4 | `src/compadre/api.py` | VTT response used `media_type="text/plain"` instead of standard `"text/vtt"` | Changed to `media_type="text/vtt"` |
| C5 | `src/compadre/health.py` | `_get_memory_mb()` assumed `ru_maxrss` is always KB — on macOS/BSD it is bytes, under-reporting by 1024× | Platform-aware conversion: macOS divides by `1024²`, Linux by `1024` |
| C6 | `src/compadre/rate_limit.py` | Constant `_SPEECH_PER_IP_LIMIT` retained legacy TTS name | Renamed to `_TRANSCRIPTION_PER_IP_LIMIT` |
| C7 | `tests/test_preprocessing_audio.py` | Four tests asserted `0.0` for ffprobe failure cases | Updated assertions to expect `None` |
| C8 | `tests/test_api.py` | VTT format test expected `media_type="text/plain"` | Updated to expect `"text/vtt"` |

## Discrepancies Found

1. **AGENTS.md** referenced `service.py` (line 43) and file list included "service" — corrected to `stt.py` to match actual codebase.
2. **monetization.md** still referenced `github.com/galvani4987/comadre` in privacy policy template — updated to `github.com/docg1701/compadre`. Also contained 14 instances of "Comadre" (missing 'p') in LGPD section — all corrected to "Compadre".
3. **proxmox-vm-setup.md** claimed ARM was unsupported due to Qwen3 binary — corrected: faster-whisper supports ARM64, Oracle Ampere A1 is valid.
4. **README.md** had no "Last updated" date — added.
5. **deployment-guide.md** still referenced `compadre-audio-cleanup` cron — preserved as it may still exist in bootstrap (operator should verify).
6. **api-architecture.md** and **api-reference.md** health response included `"compute_type"` and `"cpu_threads"` in `engines.whisper` — these fields do not exist in `src/compadre/health.py::build_health_response`. Removed from docs to match reality.
7. **api-reference.md**, **api-architecture.md**, **local-testing-guide.md**, **configuration-reference.md**: "omit for auto-detect" for `language` was incorrect. Omitting the parameter uses the configured default (`pt`); auto-detect requires passing an empty string. Corrected in all four docs.
8. **Source code stale references** (pre-existing, not doc-related): `exceptions.py` contained dead TTS classes `VoiceNotFoundError` and `GenerationError`; `main.py`, `rate_limit.py`, `keys/auth.py` contained stale `/v1/audio/speech` and TTS-specific comments. All fixed.
9. **local-testing-guide.md** expected health response included `"compute_type"` and `"cpu_threads"` — fields that do not exist in `src/compadre/health.py`. Removed.
10. **troubleshooting.md** "for both engines" heading was a TTS leftover; "Model download fails" step referenced `<model-url>` which doesn't exist for STT (faster-whisper uses HuggingFace Hub). Both fixed.
11. **deployment-guide.md** claimed bootstrap installs "two" cron jobs but actually installs three (`compadre-audio-cleanup` is still present as a legacy cron). Corrected to three.
12. **plan-01.md** Task 2 and Task 3 incorrectly described `language` behavior ("omit for auto-detect" and `""` for auto-detect at config level). Corrected to match actual code behavior.
13. **Source code bug** (pre-existing): `api.py` passed `language=""` (empty string from multipart form) directly to `faster-whisper`, which expects `None` for auto-detect. Fixed `api.py` to treat empty string as `None`.
14. **Source code bug** (pre-existing): `config.py` `whisper_language` field parsed empty env var as `""` instead of `None`, making auto-detect impossible via configuration. Added `_empty_language_to_none` validator.

15. **Missing "Last updated" dates** in `monetization.md`, `proxmox-vm-setup.md`, `rate-limiting.md`, `troubleshooting.md` — added on 2026-04-27.
16. **AGENTS.md** contained a "Kokoro auto-download" parenthetical in the model lifecycle description — removed the brand name while keeping the pattern explanation.
17. **plan-01.md** validation checklist had all 14 items unchecked — updated to checked with notes explaining which hits are expected in historical/plan documents.

## Plan Completion Status

| Plan | Status | Completed | Notes |
|------|--------|-----------|-------|
| plan-01 (Documentation Audit & Correction) | ✅ **COMPLETE** | 2026-04-27 | All 15 docs verified, 8 code fixes applied, 17 discrepancies resolved. Validation checklist: 14/14 passed. |
| plan-03 (Ansible Deploy Audit) | ⏳ Pending | — | Next priority when work resumes. |
| plan-07 (Source Code Stale Reference Cleanup) | ⏳ Pending | — | Partially addressed during plan-01 (exceptions.py, rate_limit.py, api.py fixes). Remaining: test_non_regression docs, CLI helpers review. |

## Decisions Made

- Historical documents (brief, development-plan, checkpoint) were preserved with banners rather than rewritten — they have provenance value.
- Privacy policy templates in monetization.md retain Portuguese wording (intentional for LGPD audience).
- `compadre-audio-cleanup` cron reference was left in deployment-guide.md with an explicit "legacy from the prior TTS project" note — operators need to know why the cron exists even on STT deployments.
- `deployment-guide.md` hit for "prior TTS project" is acceptable context, not a stale reference — it explains legacy behavior.
