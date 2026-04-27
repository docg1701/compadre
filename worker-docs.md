# Worker Docs — README.md and CLAUDE.md Rewrite for Compadre (STT)

## Summary

Rewrote both `README.md` and `CLAUDE.md` for Compadre, adapting from the Comadre (TTS) templates while preserving all infrastructure, security, and operational content verbatim.

## README.md — Changes from Comadre Template

| Section | Action | Details |
|---|---|---|
| Title + badges | Adapted | "Compadre", STT badge (faster-whisper large-v3-turbo), version 1.0.0 |
| Tagline | New | "Compadre Ouve Mensagem Por Áudio, Devolve Resposta Escrita" (recursive acronym from brief) |
| Description | Adapted | Self-hosted STT, faster-whisper large-v3-turbo int8, CPU-only, OpenAI-compatible |
| Why Compadre | Rewritten | STT economics: 2 replicas ~$21/mo vs OpenAI Whisper API ~$300/mo vs ElevenLabs Scribe ~$250/mo at 100k msgs/month (~14x/12x cheaper). Crossover at ~20–25 audio-hours/month. |
| Quick Start | Adapted | compadre-stt service name, cmpd_live_ key prefix, multipart/form-data curl examples for text/json/verbose_json/srt |
| How It Works | Rewritten | STT pipeline: auth → rate-limit → FIFO lock → ffmpeg decode → faster-whisper transcribe → format output → return text |
| Engine | New (replaces Dual-Engine) | Single-engine table: large-v3-turbo, CTranslate2 int8, beam_size=5, word_timestamps=True, condition_on_previous_text=False, language=pt default |
| Response Formats | New | Table of 5 formats: text, json, verbose_json, srt, vtt with Content-Types |
| Key Features | Adapted | Removed sigla/voice/TTS features; kept all billing/auth/deploy/LGPD/monitoring features verbatim (compadre-keys renamed) |
| Requirements | Adapted | 3 GB disk for model (vs 2 GB TTS models), ffmpeg in container |
| Deployment | Preserved | Same ansible playbook names (bootstrap, update, health, cleanup) |
| Documentation | Adapted | Removed TTS-specific doc references (feasibility-report, espeak-ng, qwen3, voice-management, cost-comparison-report, replica-scaling-report); kept billing/deploy/monitoring docs |
| Load Testing | Adapted | cmpd_live_ key prefix |
| Development | Preserved | Same uv/pytest/ruff/mypy commands |
| Roadmap | New | v1.1 multi-speaker diarization with speaker enrollment |
| License | Preserved | MIT |

## CLAUDE.md — Changes from Comadre Template

| Section | Action | Details |
|---|---|---|
| Project Overview | Adapted | STT instead of TTS, single-engine faster-whisper instead of dual-engine Kokoro+Qwen3 |
| Language Policy | Preserved | Identical |
| Build & Test | Preserved | Identical (uv, pytest, ruff, mypy) |
| Architecture | Rewritten core, preserved infra | Removed: dual-engine, voice discovery, engine-specific preprocessing/post-processing, siglas, Kokoro/Qwen3 details. Added: faster-whisper engine parameters (beam_size, word_timestamps, condition_on_previous_text, language, threads), audio preprocessing (ffmpeg decode → 16kHz mono), response formatting (5 formats), model lifecycle (auto-download, singleton), upload limits (25 MB / 10 min). Preserved verbatim: FIFO queue, Caddy LB, rate limiting, HTTPS, landing page pattern, import graph. |
| Code Conventions | Adapted | `CompadreError` (was `ComadreError`), everything else identical |
| Key File Locations | Adapted | Removed: `deploy/espeak-ng/pt_extra`, `deploy/bin/qwen3-tts-cli`. Added: none (model cache handled by HuggingFace hub). Renamed: `src/comadre/` → `src/compadre/`, `comadre-keys` → `compadre-keys`. |
| Non-Obvious Gotchas | Mixed | **Added STT-specific**: faster-whisper model auto-download, condition_on_previous_text non-negotiable, ffmpeg availability, upload limits, large-v3-turbo single-model MVP, word timestamps always enabled. **Removed TTS-specific**: misaki/ESPEAK_DATA_PATH, Qwen3 -l pt, Qwen3 4-core sweet spot, noise calibration per engine, kokoclone legacy, qwen3-tts-cli build, Qwen3 GGUF download, models auto-download (replaced by STT version). **Preserved verbatim** (renamed comadre→compadre): suspension sweep predicate, health exit codes, LGPD tombstone, export-customer decrypt invariant, rotate-encryption-key, backup tarball naming, BACKUP_PASSPHRASE, restore-keys.yml, ansible health.yml, bootstrap.yml chmod, cron-installed rows, export-customer --include-audit, correct-tax-id, mark-paid --months, revenue --as-of, update_customer notes_op, export-customer envelope truncation signals. |

## Verification

- README.md: 210 lines, 13 top-level sections matching Comadre structure
- CLAUDE.md: 94 lines, 6 top-level sections matching Comadre structure
- All comadre→compadre renames consistent: service `compadre-stt`, CLI `compadre-keys`, key prefix `cmpd_live_`, error class `CompadreError`, env var prefix `COMPADRE_`
- All TTS-specific content removed (dual-engine, voices, espeak-ng, qwen3, siglas, phonemes, post-processing pipelines)
- All STT-specific content added (faster-whisper engine, ffmpeg preprocessing, response formats, upload limits)
- All infrastructure/auth/billing/deploy/LGPD/monitoring content preserved verbatim
- All English, matching project language policy
