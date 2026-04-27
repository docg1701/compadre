# Compadre — Master Plan Index

**Generated:** 2026-04-26

This directory contains 7 sequential implementation plans for the Compadre STT project.
Each plan is self-contained and executable by a senior developer.

---

## Plans

| # | Plan | File | Effort | Focus |
|---|------|------|--------|-------|
| 1 | Documentation Audit & Correction | [plan-01.md](plan-01.md) | 4–6h | Fix stale Comadre TTS refs, update all docs |
| 2 | API Functional Validation | [plan-02.md](plan-02.md) | 6–10h | Test all endpoints, formats, auth, limits |
| 3 | Ansible Hardening & Fresh Deploy | [plan-03.md](plan-03.md) | 8–12h | Make all playbooks work on fresh VPS 10.10.10.208 |
| 4 | CLI Exhaustive Testing | [plan-04.md](plan-04.md) | 6–8h | Test all 21 compadre-keys verbs + edge cases |
| 5 | Security Penetration Testing | [plan-05.md](plan-05.md) | 6–10h | Black-box pentest on landing page + API |
| 6 | Faster CPU STT Engine Research | [plan-06.md](plan-06.md) | 3–5d | Find CPU-only path from RTF 1.15 → ≤ 0.30 |
| 7 | CI Pipeline Fix & Validation | [plan-07.md](plan-07.md) | 6–8h | Fix failing CI, add parallel jobs + gates |

---

## Execution Order

```
Plan 1 (Docs)
  ├─→ Plan 2 (API Tests)          [can start after Plan 1 scope is clear]
  ├─→ Plan 3 (Ansible/Deploy)     [needs Plan 1 for correct env vars]
  │     ├─→ Plan 4 (CLI Tests)    [needs fresh deployed VPS]
  │     ├─→ Plan 5 (Security)     [needs VPS + API behavior known]
  │     ├─→ Plan 6 (CPU Speed)    [needs VPS for real hardware benchmark]
  │     └─→ Plan 7 (CI)           [needs stable deploy]
```

**Recommended cycle strategy:**
- **Cycle 1:** Plan 1 + Plan 2 (parallel — docs and local API tests)
- **Cycle 2:** Plan 3 (deploy on VPS)
- **Cycle 3:** Plan 4 + Plan 5 + Plan 6 (parallel on VPS — CLI, security, and CPU benchmark)
- **Cycle 4:** Plan 7 (last — CI must reflect stable codebase)

---

## Current Project State

| Area | Status |
|------|--------|
| Source code (`src/compadre/`) | ✅ STT engine working (faster-whisper large-v3-turbo, int8, CPU) |
| API (`/v1/audio/transcriptions`, `/health`) | ✅ Functional |
| Key management CLI (`compadre-keys`, 21 verbs) | ✅ Ported from Comadre |
| Docker build | ✅ Builds and runs |
| Model download | ✅ HuggingFace cache working |
| Landing page | ✅ STT version (audio upload, 5 formats) |
| Caddy reverse proxy | ✅ HTTPS, security headers, bounded payload |
| Ansible playbooks (7) | ⚠️ Adapted from Comadre; need validation |
| Tests | ⚠️ 248 passed; 11 failures + 10 errors are non-regression tests that need Caddy in Docker |
| CI pipeline | ⚠️ Functional but monolithic; needs parallel jobs and non-regression gates |
| Documentation | ⚠️ Many TTS/Comadre staleness issues |

---

## Key Files Reference

| File | Purpose |
|------|---------|
| `src/compadre/main.py` | FastAPI app, lifespan, error handlers |
| `src/compadre/api.py` | Transcription endpoint route handler |
| `src/compadre/stt.py` | WhisperEngine (faster-whisper wrapper) |
| `src/compadre/config.py` | Settings (pydantic-settings, RegistrySettings split) |
| `src/compadre/exceptions.py` | CompadreError hierarchy + error envelope |
| `src/compadre/preprocessing.py` | Audio decode via ffmpeg → 16 kHz mono WAV |
| `src/compadre/rate_limit.py` | Per-key RPM limiting (`MovingWindowRateLimiter`) |
| `src/compadre/counters.py` | Per-replica request counters + snapshotting |
| `src/compadre/keys/` | Entire key management subsystem (CLI, DB, crypto, audit, billing) |
| `deploy/caddy/Caddyfile` | Reverse proxy config (HTTPS, CSP, bounded payload) |
| `deploy/ansible/playbooks/` | 7 Ansible playbooks |
| `compose.yaml` | Docker Compose v2 (compadre-stt + caddy) |
| `pyproject.toml` | Dependencies, tool config (ruff, mypy, pytest) |
| `AGENTS.md` | Project conventions, invariants, and non-obvious gotchas (⚠️ still contains some stale TTS-era refs; in scope for Plan 1) |

---

## Checkpoints

After each plan completes, create or update `docs/checkpoint-YYYY-MM-DD.md` with:
- What was completed vs. adapted from the plan
- Discrepancies found between plan assumptions and reality
- Decisions made during execution (with rationale)
- Next cycle recommendation
