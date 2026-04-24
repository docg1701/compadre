# Technical Brief — Compadre (Speech-to-Text SaaS)

**For: implementing agent**
**Author: project owner (via Claude consultation)**
**Status: brief — do NOT begin implementation until project owner clones Comadre at its 100%-validated state and points you at it.**

---

## 1. What you are building

**Compadre** is a self-hosted, CPU-only speech-to-text (STT) SaaS that transcribes WhatsApp-style voice messages from Brazilian Portuguese into text. It is the inverse counterpart to **Comadre** (the project owner's existing TTS SaaS). The brand pair is intentional: *Comadre fala, Compadre escuta.*

Target users: n8n workflows for WhatsApp/Telegram/Messenger automation in pt-BR, plus a public browser landing page for sporadic manual use and demos.

The product proposition mirrors Comadre's: **fixed VPS cost, scales by replica, network-isolated, OpenAI-compatible API**. It is not a Whisper API reseller — it is a self-hosted alternative to the OpenAI Whisper API and ElevenLabs Scribe at a fraction of the cost.

---

## 2. The reuse mandate (read this twice)

Comadre's codebase is **not a reference — it is the base**. The project owner has spent significant time hardening Comadre's security posture, billing lifecycle, key management, deploy automation, observability, and operational conventions. **All of that is to be inherited verbatim.** Compadre is Comadre with the engine swapped — nothing more, nothing less, in the MVP.

You will start from the **100%-validated, security-reviewed Comadre commit** (the project owner will indicate when this is ready). Until then: do not begin. Read the Comadre code thoroughly during that wait.

The following Comadre subsystems must be preserved with **no behavioral changes** beyond renaming to the Compadre brand:

- `src/comadre/keys/` — entire key registry, `comadre-keys` CLI (rename to `compadre-keys`), encrypted SQLite, customer/key lifecycle, billing verbs (`mark-paid`, `suspend`, `reactivate`, `at-risk`, `revenue`, `suspend-sweep`, `health`), LGPD verbs (`export-customer`, `delete-customer --hard`), encryption-key rotation, nightly encrypted backups, ansible health playbook
- `src/comadre/config.py` — pydantic-settings pattern, env var conventions
- `src/comadre/logging_config.py` — pipe-delimited structured logging
- `src/comadre/exceptions.py` — `ComadreError` hierarchy (rename to `CompadreError`); same `status_code` / `error_type` / `hint` contract
- `src/comadre/rate_limit.py` — per-key `MovingWindowRateLimiter`, key_id-stable bucket identity
- `src/comadre/counters.py` — 15s snapshot pattern, multi-replica aggregation
- `src/comadre/health.py` — health endpoint contract
- `src/comadre/main.py` — FastAPI lifespan, registry init
- Single FIFO `asyncio.Lock` queue per replica — preserved as the throughput model
- `deploy/caddy/Caddyfile` — HTTPS, `least_conn` LB, `dynamic a` upstream resolver, `@api` handle block
- `deploy/ansible/playbooks/` — bootstrap, update, health, cleanup, backup-keys, restore-keys (rename references)
- `deploy/landing/` — browser landing page pattern (see §6)
- `compose.yaml` — multi-replica scaling via `deploy.replicas`
- All operational conventions in `CLAUDE.md` — Docker Compose v2, English everywhere, `uv` only, `ruff` + `mypy --strict`, no raw `HTTPException`, no `print()`, no `os.environ` outside config, ansible become password via `-e`, etc.

**Do not rewrite anything in this list to "improve" it.** If you find a better pattern, write it down and ask. The owner has paid for the current design with months of validation.

---

## 3. The single substitution: TTS engine → STT engine

The architectural shape of Comadre's request lifecycle is:

```
auth → rate-limit → acquire FIFO lock → preprocess → engine → post-process → return audio
```

Compadre's lifecycle is the mirror:

```
auth → rate-limit → acquire FIFO lock → decode audio → engine (transcribe) → format output → return text
```

The auth, rate-limit, lock, and return wrapping are **identical Comadre code**. Only the middle three steps differ.

### Engine implementation

- **Library**: `faster-whisper` (CTranslate2 backend). Reference implementation: `~/dev/youtube-guru/youtube_pipeline/ingest.py::transcribe_audio` and `~/dev/youtube-guru/youtube_pipeline/config.py`. Extract the core `model.transcribe(...)` call and parameters; **discard everything else from youtube-guru** (no progress display, no medical dictionary, no checkpointing, no Rich Live tables, no partial saves, no retry decorator).
- **Model**: `large-v3-turbo` only. No model selector in MVP. Auto-download on first startup (mirror Comadre's Kokoro auto-download pattern in `src/comadre/download.py`).
- **Compute type**: CTranslate2 `int8` on CPU (the youtube-guru `WHISPER_COMPUTE_TYPE = "default"` resolves to int8 on CPU automatically).
- **Threads**: env-configurable, default 4 (matches Comadre's CPU profile per replica). Single FIFO lock means one transcription at a time per replica — the model can use all threads.
- **Critical parameters** (copy verbatim from youtube-guru, they are battle-tested):
  - `beam_size=5`
  - `word_timestamps=True` (cheap, enables `verbose_json` output mode)
  - `condition_on_previous_text=False` (prevents hallucination cascades on long audio — non-negotiable)
  - `language="pt"` as default; allow `language` param to override or auto-detect when omitted
- **Model load**: once at FastAPI lifespan startup, held in module-level singleton. Same pattern Comadre uses for its TTS models.

### Audio preprocessing

- **Tool**: `ffmpeg` subprocess (already required by Comadre for post-processing — no new dependency).
- **Operation**: decode any input format (OPUS in OGG, MP3, M4A, WAV) → 16 kHz mono PCM s16le WAV in a tempfile. Reference: `~/dev/youtube-guru/youtube_pipeline/ingest.py::extract_audio`.
- **Limits** (MVP): max upload 25 MB, max duration 10 minutes (validated via `ffprobe` before invoking the engine). Reject with `CompadreError` subclass on violation.

### Output formatting

Support OpenAI-compatible `response_format` values:

- `text` (default) — plain string in body, `Content-Type: text/plain`
- `json` — `{"text": "..."}`
- `verbose_json` — `{"text", "language", "duration", "segments": [{"start", "end", "text", "words": [...]}]}`
- `srt` — SRT subtitle format
- `vtt` — WebVTT subtitle format

Reuse the SRT formatting helper from `~/dev/youtube-guru/youtube_pipeline/srt_utils.py` (single function, no further dependencies).

---

## 4. API contract

**Single endpoint**, OpenAI Whisper API drop-in replacement:

```
POST /v1/audio/transcriptions
Authorization: Bearer cmpd_live_<plaintext>
Content-Type: multipart/form-data

file=@audio.opus            (required)
model=whisper-large-v3-turbo (optional; ignored — only one model exists, accept for OpenAI-SDK compatibility)
language=pt                  (optional; default auto-detect)
response_format=text         (optional; default text)
temperature=0                (accept for compat; ignored in MVP)
```

Key prefix: `cmpd_live_...` (Compadre brand; mirrors Comadre's `cmdr_live_...`).

Health endpoint: `GET /health` — same contract as Comadre.

OpenAPI docs at `/docs` — same as Comadre.

---

## 5. What to remove from the Comadre fork

When cloning Comadre as the starting point, **delete cleanly** (do not leave dead code):

- `src/comadre/preprocessing.py` (`normalize_text`, `chunk_text`, `expand_siglas`) — TTS-specific
- All Kokoro-related code, model files, ONNX dependencies
- All Qwen3-TTS code, the `deploy/bin/qwen3-tts-cli` binary, `docs/building-qwen3-tts-cli.md`, `docs/qwen3-tts-internals.md`, `docs/quantizing-qwen3-models.md`
- The `voices/` directory and entire voice discovery system
- `deploy/espeak-ng/` directory and the espeak-ng custom dictionary system
- All "noise mixing" / radio-warm / whatsapp-subtle post-processing pipelines
- `POST /v1/audio/speech` endpoint (replaced by `/v1/audio/transcriptions`)
- TTS-specific docs: `docs/cost-comparison-report.md`, `docs/relatorio-comparativo-custos.md`, `docs/replica-scaling-report.md`, `docs/feasibility-report-tts-engine.md`, `docs/voice-management.md`, `docs/espeak-ng-custom-dictionary.md` (write Compadre equivalents from scratch later — do not adapt)

Trim `pyproject.toml` dependencies accordingly. Add only what `faster-whisper` requires (it pulls `ctranslate2`, `tokenizers`, `huggingface_hub`).

---

## 6. Browser landing page (mandatory in MVP)

Comadre exposes a public landing page at `/` via Caddy `file_server`, serving `deploy/landing/`. Compadre needs the same pattern, with a UI inverted to match the use case:

- **Same architectural pattern**: static HTML/JS in `deploy/landing/`, bind-mounted at `/srv/www:ro`, served by Caddy at `/`, API paths routed via `@api` handle block. **Do not invent a different pattern.**
- **UI**: file input or drag-and-drop area for audio upload, API key field, "Transcrever" button, result text area below, copy-to-clipboard button. Brazilian Portuguese UI strings.
- **Auth model**: bring-your-own API key (same as Comadre's landing page — owner does not store keys client-side, user pastes their own each session).
- **Purpose**: sporadic manual use and demos. Not a customer-facing dashboard. Same scope as Comadre's landing page.

---

## 7. MVP scope (definition of done)

The MVP is "feature parity with Comadre's operational maturity, plus working transcription". Concretely:

- [ ] `POST /v1/audio/transcriptions` accepts WhatsApp `.opus` files and returns text in pt-BR
- [ ] All five `response_format` values supported (`text`, `json`, `verbose_json`, `srt`, `vtt`)
- [ ] `compadre-keys` CLI feature-complete with the verb set Comadre ships at 100% validation (every verb listed in §2)
- [ ] Per-key rate limiting via existing Comadre `rate_limit.py` — bucket on `key_id`, fed by `rate_limit_rpm` from the registry
- [ ] Counters snapshot every 15s, multi-replica aggregation works (verify with `compadre-keys cache-status`)
- [ ] Browser landing page at `/` works for upload → transcribe → show text
- [ ] Caddy HTTPS termination + `least_conn` LB across `deploy.replicas` ≥ 2 verified
- [ ] All ansible playbooks pass: `bootstrap.yml`, `update.yml`, `health.yml` (assertion chain), `cleanup.yml`, `backup-keys.yml`, `restore-keys.yml`
- [ ] `compadre-keys health --format json` returns `is_healthy=true` with all checks green
- [ ] Test suite mirrors Comadre's structure; mocked unit tests + `slow` marker for integration tests that load the model
- [ ] `ruff check .` clean, `mypy src/` strict mode clean, pre-commit hooks pass
- [ ] README.md reuses Comadre's structure (cost table, architecture diagram, deployment block, docs index) — adapted for STT economics vs Whisper API / ElevenLabs Scribe pricing
- [ ] Same FIFO-lock-per-replica scaling model — verified by load test analogous to Comadre's `scripts/loadtest.py` and `scripts/capacity_test.py`

**Billing for MVP**: stay with `rate_limit_rpm` only. Do not introduce `audio_minutes_per_month` metering yet — that is a v1.1 decision once real usage data exists. This keeps the registry schema unchanged and avoids a v002 migration in the MVP cycle.

---

## 8. Post-MVP roadmap (informational only — do NOT implement in MVP)

Single planned extension: **multi-speaker transcription with speaker diarization**. Audio with two or more speakers should be:

1. Diarized (who-speaks-when) by voice timbre
2. Returned as a structured dialogue: `[{"speaker": "S1", "start": ..., "end": ..., "text": "..."}, {"speaker": "S2", ...}]`
3. Optionally with speaker enrollment (the operator pre-registers known voices so output uses real names instead of `S1`/`S2`)

Likely tooling (subject to research at that time): `pyannote.audio` for diarization, voice embedding similarity for enrollment matching. Out of MVP scope. Mention in the post-MVP section of the Compadre README so users know it is planned.

---

## 9. Hardware and performance targets

- **Reference VPS**: 4 vCPU, 8 GB RAM (Comadre's reference hardware) — Compadre must run comfortably here with one replica.
- **Owner's home VPS**: 16 GB RAM dedicated — must support 3–4 replicas of Compadre in parallel (target capacity figure).
- **Per-replica RAM**: ~1.5–2 GB (model + Python + FastAPI overhead).
- **Realtime factor target**: ≤ 0.3 on 4 vCPU (transcribe a 1-minute audio in ≤ 18 seconds).
- **Typical WhatsApp audio (30s) latency target**: ≤ 10 seconds end-to-end.
- **Concurrency model**: one transcription per replica at a time (FIFO lock); horizontal scaling via Caddy LB across replicas — exactly Comadre's model.

---

## 10. Source material the agent must read

Before writing any code, read fully:

- **Comadre repo at its 100%-validated commit** (owner will indicate path, likely `~/dev/comadre/`). Read every file in `src/comadre/`, `deploy/`, `compose.yaml`, `Dockerfile`, `pyproject.toml`, `CLAUDE.md`, `README.md`, and the entire `docs/` directory. The architectural decisions captured in `CLAUDE.md`'s "Non-Obvious Gotchas" section apply to Compadre verbatim except where they reference TTS-specific files.
- **youtube-guru transcription extract**: only these two files, only as parameter reference for the `model.transcribe(...)` call:
  - `~/dev/youtube-guru/youtube_pipeline/ingest.py` (functions `extract_audio` and `transcribe_audio` only)
  - `~/dev/youtube-guru/youtube_pipeline/config.py` (Whisper-related constants only)
  - `~/dev/youtube-guru/youtube_pipeline/srt_utils.py` (SRT formatting helper)
  - **Ignore everything else in youtube-guru.** Its medical dictionary, checkpoint system, VAD, and pipeline orchestration are out of scope.

---

## 11. Naming conventions

- Project name: **Compadre**
- Python package: `compadre`
- CLI binary: `compadre-keys`
- Key prefix: `cmpd_live_` (test prefix `cmpd_test_` if Comadre uses an analogous test prefix)
- Env var prefix: `COMPADRE_` (mirror Comadre's `COMADRE_` convention)
- Docker service names: `compadre-stt` (vs Comadre's `tts-service`), `caddy`
- Registry DB env var: `KEY_REGISTRY_DB_PATH` (keep variable name — same registry contract; only the deployed value differs)
- Tagline (recursive acronym, mirrors Comadre's pattern): **Compadre Ouve Mensagem Por Áudio, Devolve Resposta Escrita** (Comadre = "Comadre Manda Áudio Direto, Rápido e Eficiente").

---

## 12. Critical don'ts

- **Do not** add a custom-vocabulary / boost-words / dictionary feature in MVP. Owner explicitly rejected it.
- **Do not** add speaker diarization in MVP. Owner explicitly deferred to post-MVP.
- **Do not** add multiple model sizes / model selection. `large-v3-turbo` only.
- **Do not** add audio-minute billing or registry schema migrations in MVP.
- **Do not** add VAD chunking, noise reduction, or audio enhancement preprocessing — `ffmpeg` decode to 16 kHz mono is the entire preprocessing stage.
- **Do not** add a customer-facing dashboard. The landing page is a sporadic-use demo, mirroring Comadre's pattern.
- **Do not** "modernize" or refactor any Comadre code you copy. If a pattern looks odd, it is almost certainly load-bearing — see Comadre's `CLAUDE.md` "Non-Obvious Gotchas". Ask before changing.
- **Do not** start before the owner confirms Comadre is at its 100%-validated commit and points you at the path.

---

## 13. Decision log (for the implementing agent's record)

These are decisions already made by the owner — do not re-litigate:

| Decision | Choice | Rationale |
|---|---|---|
| Model | `large-v3-turbo` | Best speed/accuracy tradeoff for WhatsApp conversational pt-BR; 16 GB RAM target supports 3–4 replicas |
| Engine | `faster-whisper` (CTranslate2) | Already proven on CPU in `youtube-guru`; superior to `whisper.cpp` for this use case due to mature Python API and word-timestamps |
| Repo strategy | New repo, fork from Comadre at 100%-validated state | Maximum reuse of validated security/billing/deploy code |
| Dictionary feature | Out of scope | WhatsApp audio is non-mission-critical; complexity not justified |
| Diarization | Post-MVP only | Single-speaker transcription is the first product |
| Billing | Per-key `rpm` only in MVP | Simplest possible billing; refine when usage data exists |
| Brand | Compadre (irmão de Comadre) | Pair branding for product family |
| Language focus | pt-BR primary; auto-detect supported | Same audience as Comadre (WhatsApp Brasil) |

---

**End of brief.** Hand this to the implementing agent together with a path to the validated Comadre repo when ready.
