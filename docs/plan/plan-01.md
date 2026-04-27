# Implementation Plan 01 — Documentation Audit & Correction

## Goal

Audit every documentation file in `docs/` and `README.md` (plus `AGENTS.md`) for stale Comadre TTS references, incorrect env vars, wrong API endpoints, and inconsistent branding. Produce a corrected, English-only, STT-consistent documentation set with a master audit checklist.

## Context

Compadre was forked from **Comadre** (a TTS project using Kokoro, Qwen3, espeak-ng, voice selection, post-processing pipelines). Many docs still describe TTS behavior — `POST /v1/audio/speech`, voice lists, `DEFAULT_VOICE`, `VOICES_DIR`, Kokoro/Qwen3 URLs, espeak-ng compilation, etc. The project language policy is **English everywhere**. The codebase is already functional; the docs lag behind.

## Scope

| In scope | Out of scope |
|----------|--------------|
| `docs/*.md` (all 13 files) | Coding, code changes |
| `README.md` | Adding new docs |
| `AGENTS.md` (repo root) | Translating LGPD privacy templates (they are intentionally Portuguese for the audience — ANPD/customers) |
| Brand, endpoint, env-var, and engine corrections | Rewriting content that's correct as-is |

## Success Criteria

1. Zero references to `Kokoro`, `Qwen3`, `espeak-ng`, `voice` (TTS sense), `synthesis`, `TTS engine`, `DEFAULT_VOICE`, `VOICES_DIR`, `NOISE_DIR` in any doc
2. All endpoint references use `POST /v1/audio/transcriptions` (not `/v1/audio/speech`)
3. All env var references use `COMPADRE_` prefix (not `COMADRE_`), and only STT-relevant vars exist
4. All branding uses "Compadre" / "compadre-keys" / "compadre-stt" consistently
5. Every curl example uses `-F` (multipart/form-data), not `-d` (JSON body)
6. Every doc has a "Last updated" date
7. Master audit checklist (`docs/plan/audit-checklist.md`) exists with all files marked as reviewed
8. `grep` inventory of suspected stale terms returns zero hits across all target files

---

## Grep Inventory of Suspected Stale Terms

Run these commands before and after the audit to track progress. Target: **zero hits** on all matches after corrections.

```bash
# TTS engine / model references
grep -rni "kokoro\|qwen\d\?\|qwen3\|espeak" docs/ README.md AGENTS.md

# Voice selection (TTS sense — "voice message" in WhatsApp context is OK)
grep -rni "voice.*select\|voice.*routing\|voice.*management\|voice.*cloning\|voice.*profile\|DEFAULT_VOICE\|VOICES_DIR\|voice.*name\|\.npy" docs/ README.md AGENTS.md

# Old endpoint
grep -rni "/v1/audio/speech" docs/ README.md AGENTS.md

# Old TTS env vars
grep -rni "KOKORO_\|QWEN3_\|ESPEAK_\|NOISE_DIR\|VOICES_DIR\|DEFAULT_VOICE\|COMADRE_" docs/ README.md AGENTS.md

# Old brand naming
grep -rni "\bcomadre\b" docs/ README.md AGENTS.md

# JSON body examples (should be multipart in STT docs)
grep -rni '"input".*:.*".*"' docs/api-reference.md docs/deployment-guide.md docs/local-testing-guide.md

# Synthesis / generate speech references
grep -rni "synthesis\|text-to-speech\|generate.speech\|generated.audio\|speech endpoint\|speech request" docs/ README.md AGENTS.md

# Old source paths (src/comadre)
grep -rni "src/comadre" docs/ README.md AGENTS.md

# Old service/container name
grep -rni "comadre-stt\|comadre-keys\b" docs/ README.md AGENTS.md
```

---

## Tasks

### Task 1 — `docs/api-architecture.md` (Complete Rewrite)

**File:** `docs/api-architecture.md`
**Effort:** 2 hours
**Current state:** 100% Comadre TTS. Describes Kokoro/Qwen3 dual-engine, `POST /v1/audio/speech`, voice routing, espeak-ng preprocessing, radio_warm/whatsapp-subtle post-processing, Qwen3 voice cloning, TTS Docker Compose.

**Changes:**
- Replace title: "Compadre STT API & Architecture Specification"
- Replace Overview: single-engine STT (faster-whisper large-v3-turbo, int8, CPU-only)
- Replace all sections:
  - **API Endpoint**: `POST /v1/audio/transcriptions` with multipart/form-data (file, model, language, response_format, temperature)
  - **Request parameters table**: file (required, audio upload), model (ignored), language (default `pt`), response_format (text/json/verbose_json/srt/vtt), temperature (ignored)
  - **Processing pipeline**: auth → rate-limit → acquire lock → ffmpeg decode to 16kHz mono WAV → faster-whisper transcribe → format output
  - **Engine parameters**: beam_size=5, word_timestamps=True, condition_on_previous_text=False, language="pt"
  - **Audio limits**: 25 MB max upload, 10 min max duration
  - **Response formats**: text, json, verbose_json, srt, vtt with Content-Type
  - **Error codes**: 400 (bad format/oversize/duration), 401, 404, 405, 429, 500, 503
  - **Health endpoint**: `{"status":"healthy","models":["large-v3-turbo"],"engines":{...},"queue_depth":0,...}`
  - **Queue architecture**: single FIFO asyncio.Lock per replica
  - **Capacity per VPS**: RTF ≤ 0.3, throughput ~5 audio-min transcribed per wall-clock minute per replica
  - **Horizontal scaling**: Caddy least_conn across replicas, dynamic a resolver
  - **Docker Compose**: single service `compadre-stt` + Caddy, volumes (compadre-models, compadre-registry, caddy-data, caddy-config)
  - **File dependencies**: large-v3-turbo (~1.5GB), ffmpeg
- Remove: all TTS sections (voice routing, Kokoro preprocessing, Qwen3 preprocessing, post-processing pipelines, speed parameter, raw parameter, character limits, Qwen3 voice configuration, Qwen3 file dependencies, Kokoro file dependencies)
- **Acceptance:** The document accurately describes the current STT pipeline as implemented in `src/compadre/`. `grep -c "kokoro\|qwen\|/v1/audio/speech\|voice.*routing\|espeak" docs/api-architecture.md` returns zero.

---

### Task 2 — `docs/api-reference.md` (Complete Rewrite)

**File:** `docs/api-reference.md`
**Effort:** 2 hours
**Current state:** 100% Comadre TTS. Auth for `/v1/audio/speech`, JSON body parameters (input, voice, speed, raw), voice routing table, engine-specific preprocessing, post-processing pipelines, voice cloning, curl examples with `-d`.

**Changes:**
- Replace title: "API Reference"
- Replace Authentication section: Bearer token for `POST /v1/audio/transcriptions` (same registry-backed auth, different endpoint)
- Replace main endpoint section:
  - **`POST /v1/audio/transcriptions`**: multipart/form-data
  - **Headers**: `Authorization: Bearer cmpd_live_<plaintext>`, `Content-Type: multipart/form-data`
  - **Form fields**:
    | Field | Type | Required | Default | Description |
    |---|---|---|---|---|
    | `file` | binary | Yes | — | Audio file (OPUS, MP3, M4A, WAV, WebM) |
    | `model` | string | No | `whisper-large-v3-turbo` | Accepted for OpenAI SDK compat; ignored |
    | `language` | string | No | `pt` | ISO 639-1 code; omit for auto-detect |
    | `response_format` | string | No | `text` | `text`, `json`, `verbose_json`, `srt`, `vtt` |
    | `temperature` | float | No | `0.0` | Accepted for compat; ignored |
  - **Success responses**: all 5 formats with curl examples using `-F`
  - **Error responses**: 400 (file too large >25MB, audio too long >10min, unsupported format, missing file), 401 (missing/invalid/suspended/revoked key), 405, 429, 500, 503
- Replace `/health` section: response shape `{"status":"healthy","models":["large-v3-turbo"],"engines":{"whisper":{"loaded":true,"model":"large-v3-turbo"}},...}`
- Remove: voice routing table, engine-specific preprocessing, post-processing pipelines, voice cloning section, `speed`/`raw`/`voice` parameters, all TTS curl examples
- **Acceptance:** Every curl example is copy-paste runnable against a live Compadre instance. `grep -c "/v1/audio/speech\|kokoro\|qwen\|voice.*routing" docs/api-reference.md` returns zero.

---

### Task 3 — `docs/configuration-reference.md` (Major Update)

**File:** `docs/configuration-reference.md`
**Effort:** 1 hour
**Current state:** Lists all Comadre TTS env vars (DEFAULT_VOICE, VOICES_DIR, ESPEAK_DATA_PATH, NOISE_DIR, KOKORO_MODEL_PATH, KOKORO_VOICES_PATH, KOKORO_MODEL_URL, KOKORO_VOICES_URL, QWEN3_ENABLED, QWEN3_CLI_PATH, QWEN3_MODEL_PATH, QWEN3_TOKENIZER_PATH, QWEN3_THREADS, QWEN3_MODEL_URL, QWEN3_TOKENIZER_URL). References TTS-specific URL validation.

**Changes:**
- Update title: "Compadre Configuration Reference"
- Remove all TTS-specific env vars (list above) from the table
- Add STT-specific env vars to the table:
  | Variable | Default | Description | Example |
  |---|---|---|---|
  | `COMPADRE_WHISPER_MODEL_SIZE` | `large-v3-turbo` | faster-whisper model size | `large-v3-turbo` |
  | `COMPADRE_WHISPER_COMPUTE_TYPE` | `int8` | CTranslate2 compute type | `int8` |
  | `COMPADRE_WHISPER_CPU_THREADS` | `4` | CPU threads for inference (≥1) | `4` |
  | `COMPADRE_WHISPER_LANGUAGE` | `pt` | Default transcription language; `""` for auto-detect | `pt` |
  | `COMPADRE_HF_HOME` | `./data/cache/huggingface` | HuggingFace cache directory | `/app/models` |
  | `MAX_UPLOAD_SIZE_MB` | `25` | Max upload file size in MB (1–100) | `25` |
  | `MAX_AUDIO_DURATION_SECONDS` | `600` | Max audio duration in seconds (≥1) | `600` |
- Update `DOMAIN` example from `tts.example.com` to `stt.example.com`
- Update `OPERATOR_CONTACT_HINT` default from "Contact your Comadre operator" to "Contact your Compadre operator"
- Update compose.yaml environment block to match current `compose.yaml` (no voice/audio volumes, add compadre-models, whisper vars)
- Update `.env.example` content to match current `.env.example`
- Update auxiliary files table: remove voice-related entries, keep registry siblings (`.invalidate`, `.counters.*.json`, `.cron-installed`, `.backup-cron-installed`, `.backup-*.tar.gz.enc`)
- Remove URL validation section for TTS model URLs (keep mention that empty model URL skips download if relevant)
- **Acceptance:** Every env var listed exists in `src/compadre/config.py::Settings`. `grep -c "DEFAULT_VOICE\|VOICES_DIR\|ESPEAK\|NOISE_DIR\|KOKORO\|QWEN3" docs/configuration-reference.md` returns zero.

---

### Task 4 — `docs/deployment-guide.md` (Major Update)

**File:** `docs/deployment-guide.md`
**Effort:** 2 hours
**Current state:** Comprehensive Comadre TTS deployment guide. Title "Comadre TTS service", inventory variables include `default_voice`, `voices_local_dir`, `kokoro_model_url`, `kokoro_voices_url`, `qwen3_enabled`, `qwen3_model_url`, `qwen3_tokenizer_url`, `qwen3_model_local`, `qwen3_tokenizer_local`. References `deploy-voices.yml` playbook. Test commands use `/v1/audio/speech` with JSON body. Bootstrap output shows "Loaded Voices: kokoro/pf_dora, ...". Health report mentions "voices, engines" and Qwen3 warnings. Model deployment section describes Qwen3 GGUF conversion. Voice deployment section describes kokoro/qwen voice directory structure. Memory breakdown references Kokoro+Qwen3. Scaling section references `compadre-voices` volume.

**Changes:**
- Update title and all headings: "Compadre STT" not "Comadre TTS"
- **Inventory configuration section:**
  - Remove: `voices_local_dir`, `default_voice`, `kokoro_model_url`, `kokoro_voices_url`, `qwen3_enabled`, `qwen3_model_url`, `qwen3_tokenizer_url`, `qwen3_model_local`, `qwen3_tokenizer_local`
  - Add: `compadre_whisper_model_size` (default `large-v3-turbo`), `compadre_whisper_compute_type` (default `int8`), `compadre_whisper_cpu_threads` (default `4`), `compadre_whisper_language` (default `pt`), `compadre_hf_home`
  - Update variable descriptions table to match
  - Remove Oracle Cloud Ampere A1 note (x86_64-only restriction was Qwen3-specific; faster-whisper supports ARM64)
- **Deployment flow section:**
  - Remove step 2 (`deploy-voices.yml`)
  - Update step 4 test curl: `POST /v1/audio/transcriptions` with `-F "file=@test.opus"` not `-d '{"input":"..."}'`
- **Available playbooks table:**
  - Remove: `deploy-voices.yml` row
  - Update: `deploy-models.yml` — describe as "Pre-seed faster-whisper model cache for air-gapped deploys" (not Qwen3 GGUF)
- **Bootstrap section:**
  - Remove: "Creates empty voices directory structure" step
  - Remove: "Bootstrap does NOT deploy voice files" note
  - Update expected output: "Compadre STT Service — Bootstrap Complete", replace "Loaded Voices" with "Loaded Model: large-v3-turbo"
- **Update section:**
  - Change "Docker volumes (models, voices, audio) are not affected" → "Docker volumes (compadre-models, compadre-registry) are not affected"
- **Health section:**
  - Change "Health endpoint response (status, voices, engines, memory, uptime)" → "(status, models, engines, memory, uptime)"
  - Remove "Qwen3 engine not loaded" warning
  - Add "STT model not loaded" warning
- **Model Deployment section:**
  - Rewrite for faster-whisper model: ~1.5 GB, auto-downloads from HuggingFace on first startup, pre-seed via `deploy-models.yml` for air-gapped
  - Remove all Qwen3-specific conversion instructions, file paths, model URLs
- **Voice Deployment section:** DELETE ENTIRE SECTION
- **HTTPS section:** Update example domain from `tts.example.com` to `stt.example.com`; change "TTS service" to "STT service"
- **Landing Page section:** Update "TTS service" references to "STT service"
- **Key Registry Management:** Update "Comadre authenticates every TTS request" → "Compadre authenticates every STT request"
- **Scaling section:**
  - Update per-replica memory estimate: "Python process with faster-whisper loaded ~2 GB" (not Kokoro 4.2 GB + Qwen3 1.5 GB)
  - Remove Voices row from "What is shared vs per-instance" table
  - Update Disk row: "Shared (models volume)" not "(models + voices volumes)"
  - Remove "Qwen3 has a 4-core sweet spot" note
- **Cron Jobs section:** Verify `compadre-audio-cleanup` cron description (deletes generated audio files — confirm relevance for STT)
- **Acceptance:** The deployment guide can be followed step-by-step for a fresh Compadre deploy. `grep -c "kokoro\|qwen\|/v1/audio/speech\|deploy-voices\|DEFAULT_VOICE\|voices_local_dir\|komadre TTS" docs/deployment-guide.md` returns zero.

---

### Task 5 — `docs/troubleshooting.md` (Major Update)

**File:** `docs/troubleshooting.md`
**Effort:** 1.5 hours
**Current state:** Contains TTS-specific sections: "espeak-ng pt_extra not found", "qwen3-tts-cli binary missing", "Kokoro sigla mispronunciation", "Qwen3 random accent", "Clone voice sounds different", "404 voice not found". Startup phases describe Kokoro/Qwen3. Health response examples show `voices`/`engines` with kokoro/qwen3. Log component table references "TTS generation".

**Changes:**
- **Startup Failures section:**
  - Keep: "KEY_REGISTRY_ENCRYPTION_KEY not set"
  - Update "Model download fails": change from Kokoro/Qwen3 to faster-whisper (HuggingFace Hub, SSL issues, pre-seed via deploy-models)
  - Remove: "espeak-ng pt_extra not found" (entire section)
  - Remove: "qwen3-tts-cli binary missing" (entire section)
  - Add: "ffmpeg/ffprobe not found" — symptom, cause, resolution
- **Runtime Issues section:**
  - Remove: "404 voice not found" (entire section)
  - Keep: "503 warming_up", "401 authentication error" (update auth error hints for STT context), "429 rate limit exceeded", "compadre-keys CLI errors", "compadre-keys cache-status reports unavailable"
  - Add: "Upload size exceeded (400)" — symptom, cause (25 MB limit), resolution
  - Add: "Audio duration exceeded (400)" — symptom, cause (10 min limit), resolution
  - Add: "Unsupported audio format (400)" — symptom, cause (ffprobe rejects), resolution (use OPUS/MP3/M4A/WAV/WebM)
  - Add: "Transcription timeout (504/500)" — symptom, cause (queue backed up, audio too long), resolution
  - Add: "Empty transcription result" — symptom, cause (silence, wrong language), resolution (check language param)
  - Update "Request timeout" section: remove "max 500 chars for Kokoro, 300 chars for Qwen3"; replace with audio duration/timeout guidance
- **Engine-Specific Issues (entire section):** DELETE — no multi-engine in Compadre
- **Log Format section:**
  - Update component table: "TTS generation" → "STT transcription"; add "engine" component
- **Acceptance:** Every troubleshooting entry is relevant to the current STT codebase. `grep -c "kokoro\|qwen\|espeak\|voice.*not found\|sigla" docs/troubleshooting.md` returns zero.

---

### Task 6 — `docs/local-testing-guide.md` (Complete Rewrite)

**File:** `docs/local-testing-guide.md`
**Effort:** 1.5 hours
**Current state:** 100% Comadre TTS local testing guide. References qwen3-tts-cli binary, voice WAVs, noise WAVs, espeak-ng, Kokoro model download, Qwen3 GGUF conversion, voice testing commands.

**Changes:**
- Rewrite for Compadre STT local testing:
  - **Prerequisites**: Docker, 4 vCPU / 8 GB RAM, ~3 GB disk (model), ffmpeg in container
  - **What is already in the repo**: nothing to pre-download for testing (model auto-downloads)
  - **What must be obtained**: faster-whisper large-v3-turbo (~1.5 GB, auto-downloads on first startup)
  - **Step 1**: Create `.env` with `KEY_REGISTRY_ENCRYPTION_KEY`, plus STT env vars
  - **Step 2**: Build and start (`docker compose up --build -d`)
  - **Step 3**: Wait for startup (model download ~1-2 min, engine warm-up ~10-30s)
  - **Step 4**: Seed customer and issue key
  - **Step 5**: Test transcription with all 5 formats (curl examples using `-F`):
    - text, json, verbose_json, srt, vtt
  - **Step 6**: Test with language override, auto-detect
  - **Step 7**: Error examples (missing auth, oversized upload)
  - **Stopping and restarting**: same as before
  - **Troubleshooting**: point to `troubleshooting.md`
- Remove: all Qwen3 model conversion steps, voice testing, Kokoro/Qwen3 sections, espeak-ng compilation
- **Acceptance:** Every curl example is runnable. `grep -c "kokoro\|qwen\|espeak\|/v1/audio/speech\|voice.*test\|noise" docs/local-testing-guide.md` returns zero.

---

### Task 7 — `docs/monetization.md` (Targeted Fixes)

**File:** `docs/monetization.md`
**Effort:** 30 min
**Current state:** Mostly registry/billing logic (engine-agnostic). Contains stale: `/v1/audio/speech` references (2 occurrences), `src/comadre/` source paths (6 occurrences), "TTS engine" reference (1), "Comadre" branding (several), privacy policy template references `github.com/galvani4987/comadre` (2). Also references "Comadre 2.0.0" and "Comadre's data model".

**Changes:**
- `POST /v1/audio/speech` → `POST /v1/audio/transcriptions` (2 occurrences)
- "The TTS engine is never invoked on a 429" → "The STT engine is never invoked on a 429"
- `src/comadre/keys/audit.py` → `src/compadre/keys/audit.py` (2 occurrences)
- `src/comadre/keys/cli/commands/customers.py` → `src/compadre/keys/cli/commands/customers.py`
- `github.com/galvani4987/comadre` → `github.com/docg1701/compadre` (privacy policy template — Portuguese and English versions)
- References section: `src/comadre/keys/...` → `src/compadre/keys/...` (4 paths)
- All "Comadre" brand references → "Compadre" (privacy template software name references only; operator-owned wording stays Portuguese)
- "Comadre 2.0.0" → "Compadre 1.0.0"
- "Comadre's data model" → "Compadre's data model"
- **Do NOT change:** LGPD privacy templates Portuguese/English wording (intentionally Portuguese for audience), `_FORBIDDEN_DETAIL_KEYS` content, billing logic
- **Acceptance:** `grep -c "/v1/audio/speech\|src/comadre\|TTS engine" docs/monetization.md` returns zero. Only 1-2 "Comadre" references remain (if any, in historical context).

---

### Task 8 — `docs/rate-limiting.md` (Targeted Fixes)

**File:** `docs/rate-limiting.md`
**Effort:** 15 min
**Current state:** Mostly engine-agnostic. Contains: `POST /v1/audio/speech` reference (1), "Comadre" branding (6 occurrences).

**Changes:**
- Line 19 (pseudo-flow): `POST /v1/audio/speech` → `POST /v1/audio/transcriptions`; `generate_speech` → `transcribe_audio`; "TTS engine generates audio" → "STT engine transcribes audio"
- All "Comadre" → "Compadre": "Comadre enforces", "Today Comadre ships", "For Comadre's current throughput", "When `check_rate_limit` denies the request, Comadre returns", "At Comadre's throughput", "Comadre exposes per-key usage counters"
- **Acceptance:** `grep -ci "comadre" docs/rate-limiting.md` returns zero. `grep -c "/v1/audio/speech" docs/rate-limiting.md` returns zero.

---

### Task 9 — `docs/compadre-brief.md` (Mark Historical)

**File:** `docs/compadre-brief.md`
**Effort:** 5 min
**Current state:** Original technical brief from project inception. Contains references to Comadre fork strategy, TTS engine deletion instructions, Kokoro, Qwen3, voice cloning, `/v1/audio/speech`. These are historically correct for their time — this was a planning document.

**Changes:**
- Add a banner at the top:
  ```
  > ⚠️ **Historical Reference** — This is the original MVP technical brief (2026-03). It informed the initial
  > implementation. See [README.md](../README.md) and [api-reference.md](api-reference.md) for the current state
  > of the project. Many instructions here (deleting TTS code, renaming packages) were already executed.
  ```
- Add "Last updated: 2026-03 (historical)" date
- **Do NOT modify body content** — the historical instructions have provenance value
- **Acceptance:** Reader can immediately tell this is not current.

---

### Task 10 — `docs/compadre-development-plan.md` (Mark Historical)

**File:** `docs/compadre-development-plan.md`
**Effort:** 5 min
**Current state:** The phased implementation plan from initial development. References Comadre fork, TTS deletions, `src/comadre/` paths, `/v1/audio/speech`. These were accurate for the implementation phase.

**Changes:**
- Add a banner at the top:
  ```
  > ⚠️ **Historical Reference** — This is the implementation plan used during initial development
  > (2026-03 to 2026-04). Most tasks are complete. See [checkpoint-2026-04-26.md](checkpoint-2026-04-26.md)
  > for what remains. Do not re-execute completed tasks from this document.
  ```
- Add "Last updated: 2026-04 (historical)" date
- **Acceptance:** Reader can immediately tell this is not the current to-do list.

---

### Task 11 — `docs/proxmox-vm-setup.md` (Targeted Fixes)

**File:** `docs/proxmox-vm-setup.md`
**Effort:** 15 min
**Current state:** References "Comadre", VM name `comadre-tts`, `qwen3-tts-cli` binary, "TTS server VM".

**Changes:**
- Title: "Proxmox VM Setup for Compadre"
- Description: "to run Compadre" not "to run Comadre"
- VM Name: `compadre-stt` not `comadre-tts`
- "No GPU needed, Comadre runs 100% on CPU" → "No GPU needed, Compadre runs 100% on CPU"
- "Unnecessary for a TTS server VM" → "Unnecessary for an STT server VM"
- "Predictable latency for TTS inference" → "Predictable latency for STT inference"
- Remove ARM note about `qwen3-tts-cli` (or replace with note about faster-whisper CPU requirements)
- Update §5 heading: "Deploy Compadre"
- Update VM name example at end: `compadre-stt`
- **Acceptance:** `grep -ci "comadre\|qwen3-tts-cli\|TTS server" docs/proxmox-vm-setup.md` returns zero.

---

### Task 12 — `docs/checkpoint-2026-04-26.md` (Mark Historical)

**File:** `docs/checkpoint-2026-04-26.md`
**Effort:** 5 min
**Current state:** Portuguese-language checkpoint document. Accurate for its time.

**Changes:**
- Add a banner at the top:
  ```
  > ⚠️ **Snapshot** — Development checkpoint from 2026-04-26. Records what was completed
  > and what remained at that point. For current status, see the most recent checkpoint.
  ```
- Add "Language: pt-BR (checkpoint convention)" note (Portuguese is acceptable for checkpoints per the owner's convention)
- **Acceptance:** Reader can immediately tell this is a historical snapshot.

---

### Task 13 — `docs/claude-code-instruction-quality-guide.md` (Review Only)

**File:** `docs/claude-code-instruction-quality-guide.md`
**Effort:** 10 min
**Current state:** Generic LLM instruction quality reference. Contains no TTS-specific content.

**Changes:**
- Review for any `comadre` or `compadre` references — likely none
- Add "Last updated" date if missing
- Verify file is English-only
- **Acceptance:** No stale references found; mark as "clean" in audit checklist.

---

### Task 14 — `README.md` (Verify & Polish)

**File:** `README.md`
**Effort:** 30 min
**Current state:** Already mostly updated for STT (see checkpoint). Has cost comparison tables, multipart curl examples, engine parameters table. 

**Changes (verify each):**
- ✅ Cost comparison tables use STT metrics (Opus transcription, not TTS generation) — verify numbers are realistic
- ✅ Quick Start uses multipart curl commands (`-F "file=@audio.opus"`)
- ✅ Architecture diagram shows STT pipeline
- ✅ Engine parameters table matches current `src/compadre/stt.py`
- ✅ Response formats table is correct
- ✅ Deploy section uses correct env var names
- 🔍 Check for residual: any `comadre` reference, Portuguese tagline "Compadre Ouve Mensagem Por Áudio, Devolve Resposta Escrita" (tagline is acceptable per brand), any stale anchor links
- 🔍 Check the tagline at top: "Compadre Ouve Mensagem Por Áudio, Devolve Resposta Escrita" — this is the project's intentional Brazilian Portuguese recursive acronym; keep it but add English translation in parentheses
- 🔍 Verify docs index links are all correct (no dead links to deleted docs)
- Add "Last updated" date
- **Acceptance:** README renders correctly. `grep -c "comadre\b" README.md` returns zero (case-insensitive). Every command in Quick Start is copy-paste runnable.

---

### Task 15 — `AGENTS.md` (Root Repo) (Verify & Fix)

**File:** `AGENTS.md`
**Effort:** 20 min
**Current state:** Contains architecture overview, invariants, and gotchas. References `src/comadre/` paths (historical, from Comadre inheritance). May contain outdated file paths.

**Changes:**
- Verify file paths reference `src/compadre/` not `src/comadre/`
- Verify "Non-Obvious Gotchas" are current — remove any TTS-specific gotchas (espeak-ng, voice-related)
- Verify import graph documentation: `preprocessing.py → stt.py` (not `service.py`)
- Verify service name references: `compadre-stt` not `comadre-stt`
- Check for env var prefix: `COMPADRE_` not `COMADRE_`
- Verify STT-specific invariants are documented (condition_on_previous_text, beam_size, etc.)
- Verify file location table mentions `stt.py` (not `service.py`), `preprocessing.py` (audio decode, not text normalization), etc.
- **Acceptance:** AGENTS.md accurately describes the current codebase. A new developer can read it and understand the project.

---

### Task 16 — Produce Master Audit Checklist

**File:** `docs/plan/audit-checklist.md` (new)
**Effort:** 15 min
**Current state:** Does not exist.

**Changes:**
- Create a markdown table with columns:
  | # | File | Status | Issues Found | Fixed | Reviewer | Date |
  |---|------|--------|-------------|-------|----------|------|
- Pre-populate with all 15 files above + README + AGENTS.md
- Fill in status for each as you complete the audit
- **Acceptance:** Checklist exists and all rows show "passed" after all tasks complete.

---

## Files to Modify

| File | Type of Change | Effort |
|------|---------------|--------|
| `docs/api-architecture.md` | Complete rewrite | 2h |
| `docs/api-reference.md` | Complete rewrite | 2h |
| `docs/configuration-reference.md` | Major update | 1h |
| `docs/deployment-guide.md` | Major update | 2h |
| `docs/troubleshooting.md` | Major update | 1.5h |
| `docs/local-testing-guide.md` | Complete rewrite | 1.5h |
| `docs/monetization.md` | Targeted fixes (endpoints, paths, brand) | 30m |
| `docs/rate-limiting.md` | Targeted fixes (endpoints, brand) | 15m |
| `docs/compadre-brief.md` | Add historical banner | 5m |
| `docs/compadre-development-plan.md` | Add historical banner | 5m |
| `docs/proxmox-vm-setup.md` | Targeted fixes (brand, VM name) | 15m |
| `docs/checkpoint-2026-04-26.md` | Add snapshot banner | 5m |
| `docs/claude-code-instruction-quality-guide.md` | Review only | 10m |
| `README.md` | Verify & polish | 30m |
| `AGENTS.md` | Verify & fix paths/invariants | 20m |

## New Files

| File | Purpose |
|------|---------|
| `docs/plan/audit-checklist.md` | Master tracking sheet — one row per doc, status + issues + resolution |

## Dependencies

- **No upstream dependencies.** This plan should be executed first, before any other plan, since corrected docs inform all subsequent work.
- Task order is not strict, but Tasks 1-5 (the "full rewrite" docs) are the highest effort; start there.
- Task 16 (audit checklist) should be created early and filled in progressively.

## Risks

| Risk | Likelihood | Mitigation |
|------|-----------|------------|
| Monetization.md LGPD privacy templates reference `github.com/galvani4987/comadre` — changing to `compadre` may affect operator privacy policies that copied the template | Low (docs not yet deployed to real operators) | Change URL to point at the Compadre repo; add a note that operators should substitute their own repo URL |
| Rewritten docs may describe the system as it *should* be, not as it *is* | Medium | Cross-check against actual source code (`src/compadre/`) before finalizing each doc |
| Local-testing-guide.md may describe ffmpeg/model paths that differ from actual container layout | Medium | Verify by running the described commands against a live Compadre container |
| Some "comadre" references in historical docs (brief, dev-plan) are accurate in context | Low | Preserve historical docs with banners instead of modifying body content |
| Monetization.md contains line-number-based instructions — if file grows, line numbers drift | Low | Use content-based search (`/v1/audio/speech`, `src/comadre`) rather than line numbers during edits |
| README cost comparison tables may have inaccurate numbers | Low | Verify load-test results before adjusting numbers |
| `compadre-keys` vs `compadre-keys` confusion — the brief says rename, but actual code uses `compadre-keys` | None (already `compadre-keys`) | Verify and document the actual CLI binary name |

## Validation Checklist (Post-Audit)

Run these grep commands; expect **zero hits** on each:

- [ ] `grep -rni "kokoro\|qwen3\|qwen\b" docs/ README.md AGENTS.md`
- [ ] `grep -rni "espeak" docs/ README.md AGENTS.md`
- [ ] `grep -rni "/v1/audio/speech" docs/ README.md AGENTS.md`
- [ ] `grep -rni "DEFAULT_VOICE\|VOICES_DIR\|ESPEAK_DATA_PATH\|NOISE_DIR" docs/ README.md AGENTS.md`
- [ ] `grep -rni "KOKORO_\|QWEN3_\|ESPEAK_" docs/ README.md AGENTS.md`
- [ ] `grep -rni "src/comadre" docs/ README.md AGENTS.md`
- [ ] `grep -rni "TTS engine\|TTS generation\|TTS service\|TTS request\|speech synthesis\|text-to-speech\|generate.speech\|generated.audio" docs/ README.md AGENTS.md`
- [ ] `grep -rni "voice.*routing\|voice.*select\|voice.*cloning\|voice.*management\|voice.*profile" docs/ README.md AGENTS.md`
- [ ] `grep -rni "comadre-stt\|comadre-keys\b" docs/ README.md AGENTS.md`
- [ ] `grep -rni '"input".*:.*".*"' docs/api-reference.md docs/deployment-guide.md docs/local-testing-guide.md` (JSON body example — should use multipart)
- [ ] Every doc has a "Last updated" date at the top
- [ ] `docs/plan/audit-checklist.md` exists and all rows show "passed"
- [ ] Every curl example uses `-F` (multipart), not `-d` (JSON)
- [ ] All endpoint references are `/v1/audio/transcriptions`
