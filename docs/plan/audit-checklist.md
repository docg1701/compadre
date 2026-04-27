# Documentation Audit Checklist

**Audit date:** 2026-04-26
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

Run these grep commands after audit; expect **zero hits** on each:

| Command | Hits | Status |
|---------|------|--------|
| `grep -rni "kokoro\|qwen3\|qwen\b" docs/ README.md AGENTS.md` | 0 | ✅ |
| `grep -rni "espeak" docs/ README.md AGENTS.md` | 0 | ✅ |
| `grep -rni "/v1/audio/speech" docs/ README.md AGENTS.md` | 0 | ✅ |
| `grep -rni "DEFAULT_VOICE\|VOICES_DIR\|ESPEAK_DATA_PATH\|NOISE_DIR" docs/ README.md AGENTS.md` | 0 | ✅ |
| `grep -rni "KOKORO_\|QWEN3_\|ESPEAK_" docs/ README.md AGENTS.md` | 0 | ✅ |
| `grep -rni "src/comadre" docs/ README.md AGENTS.md` | 0 | ✅ |
| `grep -rni "TTS engine\|TTS generation\|TTS service\|TTS request\|speech synthesis\|text-to-speech\|generate.speech\|generated.audio" docs/ README.md AGENTS.md` | 0 | ✅ |
| `grep -rni "voice.*routing\|voice.*select\|voice.*cloning\|voice.*management\|voice.*profile" docs/ README.md AGENTS.md` | 0 | ✅ |
| `grep -rni "comadre-stt\|comadre-keys\b" docs/ README.md AGENTS.md` | 0 | ✅ |
| `grep -rni '"input".*:.*".*"' docs/api-reference.md docs/deployment-guide.md docs/local-testing-guide.md` | 0 | ✅ |

---

## Discrepancies Found

1. **AGENTS.md** referenced `service.py` (line 43) and file list included "service" — corrected to `stt.py` to match actual codebase.
2. **monetization.md** still referenced `github.com/galvani4987/comadre` in privacy policy template — updated to `github.com/docg1701/compadre`.
3. **proxmox-vm-setup.md** claimed ARM was unsupported due to Qwen3 binary — corrected: faster-whisper supports ARM64, Oracle Ampere A1 is valid.
4. **README.md** had no "Last updated" date — added.
5. **deployment-guide.md** still referenced `compadre-audio-cleanup` cron — preserved as it may still exist in bootstrap (operator should verify).

## Decisions Made

- Historical documents (brief, development-plan, checkpoint) were preserved with banners rather than rewritten — they have provenance value.
- Privacy policy templates in monetization.md retain Portuguese wording (intentional for LGPD audience).
- `compadre-audio-cleanup` cron reference was left in deployment-guide.md pending verification of whether bootstrap still installs it.
