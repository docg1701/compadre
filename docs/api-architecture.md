# Compadre STT API & Architecture Specification

**Date:** 2026-04-26
**Status:** Definitive specification
**Last updated:** 2026-04-26

## Overview

Compadre is a self-hosted Brazilian Portuguese speech-to-text (STT) API for WhatsApp/Telegram/Messenger automation via n8n. Single-engine: faster-whisper large-v3-turbo (int8 quantized, CPU-only, CTranslate2 backend). Single FIFO queue per instance, horizontal scaling via Caddy load balancer.

## API Endpoint

### `POST /v1/audio/transcriptions`

OpenAI-compatible endpoint. n8n sends an audio file, receives transcribed text.

#### Request

Multipart/form-data upload:

```bash
curl -k -s https://stt.example.com/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_<plaintext>" \
  -F "file=@audio.opus" \
  -F "response_format=text"
```text
#### Parameters

| Parameter | Type | Required | Default | Description |
|---|---|---|---|---|
| `file` | binary | Yes | — | Audio file (OPUS in OGG, MP3, M4A, WAV, WebM) |
| `model` | string | No | `whisper-large-v3-turbo` | Accepted for OpenAI SDK compatibility; ignored |
| `language` | string | No | `pt` | ISO 639-1 language code; omit for auto-detect |
| `response_format` | string | No | `text` | `text`, `json`, `verbose_json`, `srt`, `vtt` |
| `temperature` | float | No | `0.0` | Accepted for compatibility; ignored |

#### Processing Pipeline

```
auth → rate-limit → acquire lock → ffmpeg decode (16 kHz mono PCM s16le WAV)
  → faster-whisper transcribe → format output → return
```text
**Engine parameters (non-configurable):**

| Parameter | Value | Rationale |
|---|---|---|
| `beam_size` | `5` | Quality/speed trade-off |
| `word_timestamps` | `True` | Enables `verbose_json`, `srt`, `vtt` formats |
| `condition_on_previous_text` | `False` | Prevents hallucination cascades on long audio |
| `language` | `pt` (default) | Brazilian Portuguese. Omitting the parameter uses this default; pass empty string or set env var to `""` for auto-detect. |
| `cpu_threads` | `4` (env-configurable via `COMPADRE_WHISPER_CPU_THREADS`) | Matches 4 vCPU reference hardware |

**Audio limits:**

| Limit | Value | Enforced by |
|---|---|---|
| Max upload size | 25 MB | FastAPI middleware + Caddy bounded-payload policy |
| Max audio duration | 10 minutes | `ffprobe` validation before engine invocation |

Exceeding either limit returns HTTP 400.

#### Response Formats

| Format | Content-Type | Body shape |
|---|---|---|
| `text` | `text/plain` | `"transcribed text"` |
| `json` | `application/json` | `{"text": "..."}` |
| `verbose_json` | `application/json` | `{"text", "language", "duration", "segments": [{"start", "end", "text", "words": [{"word", "start", "end", "probability"}]}]}` |
| `srt` | `text/plain` | SubRip subtitle format |
| `vtt` | `text/plain` | WebVTT subtitle format |

#### Errors

| Code | Condition |
|---|---|
| 400 | Missing file, unsupported format, file >25 MB, audio >10 min |
| 401 | Invalid/missing bearer token, or key suspended/revoked/past-due in registry |
| 404 | Endpoint not found (Caddy or app-level) |
| 405 | Method not allowed |
| 429 | Rate limit exceeded (per-key RPM or per-IP `/health` enumeration guard) |
| 500 | Transcription engine failure |
| 503 | Service warming up (model loading) |

### `GET /health`

```json
{
  "status": "healthy",
  "models": ["large-v3-turbo"],
  "engines": {
    "whisper": {
      "loaded": true,
      "model": "large-v3-turbo"
    }
  },
  "queue_depth": 0,
  "uptime_seconds": 3600,
  "memory_mb": 2100,
  "version": "1.1.0"
}
```text
### Authentication

```text
Authorization: Bearer cmpd_live_<plaintext>
```text

Keys are stored in an encrypted SQLite registry and issued via the `compadre-keys` CLI. Each key carries its own `rate_limit_rpm` and billing metadata. The registry is the sole source of truth — there is no single-key environment-variable fallback. See [monetization.md](monetization.md) and [rate-limiting.md](rate-limiting.md).

## Queue Architecture

### Design: Single FIFO queue per instance

```python
lock = asyncio.Lock()

@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile):
    async with lock:  # One transcription at a time, FIFO order
        result = await run_in_executor(transcribe, audio_path)
    return format_response(result, response_format)
```text
- No Redis, no Celery, no external queue
- `asyncio.Lock` ensures FIFO ordering
- FastAPI keeps HTTP connection open until transcription is ready
- n8n waits for response (timeout configurable, default 300s)

### Why this is sufficient

- WhatsApp conversations are async — users don't expect instant responses
- Large-v3-turbo transcribes 10s audio in ~11.5s (RTF ~1.15) on 4 vCPU reference hardware
- The queue depth is visible via `/health` endpoint for monitoring
- No complex infrastructure to maintain

### Capacity per VPS (4 vCPU, 8 GB RAM)

| Metric | Value |
|---|---|
| RTF (real-time factor) | ~1.15 (10s audio → 11.5s processing) |
| Throughput | ~5 audio-minutes transcribed per wall-clock minute |
| Max comfortable conversations | ~15-20 (assuming 30-60s audio per message, natural spacing) |

### Realistic queue behavior

Requests don't arrive simultaneously — they're spread across time because WhatsApp conversations have natural pauses (user recording, typing, reading).

**Example: 15 conversations**

- Average request arrival: 1 every 2-4 seconds
- Engine processes 1 every ~11.5s (for 10s audio)
- Queue depth oscillates between 0-3 (rarely peaks above 4)
- Typical latency: 11.5s (no queue) to 34s (2-3 in queue)

### Theoretical worst-case (all requests arrive at once)

| Queue depth | Wait time |
|---|---|
| 1 | 11.5s |
| 5 | 57.5s |
| 10 | 115s |

These worst-case numbers rarely occur in practice due to the natural spacing of messaging conversations.

## Horizontal Scaling

When a single instance isn't enough, add more VPS instances behind Caddy.

### Architecture

```text
n8n (any server)
    │
    │ POST /v1/audio/transcriptions
    ▼
Caddy (reverse proxy + load balancer)
    │
    ├── VPS 1: compadre-stt instance (own FIFO queue)
    ├── VPS 2: compadre-stt instance (own FIFO queue)
    └── VPS N: compadre-stt instance (own FIFO queue)
```text
### Caddy configuration

```caddyfile
stt.example.com {
    reverse_proxy {
        to vps1.internal:8000 vps2.internal:8000
        lb_policy least_conn
        health_uri /health
        health_interval 30s
    }
}
```text
### Scaling rules

| Audio volume | Instances |
|---|---|
| < 15 conversations | 1 |
| 15-30 | 2 |
| 30-60 | 3-4 |
| 60+ | Auto-scale |

Each instance is independent — own model loaded, own queue, own processing. No shared state. Adding/removing instances requires only Caddy config update.

### Failover pattern (n8n-side)

When a compadre instance times out, the n8n workflow handles failover — not the load balancer. This gives the workflow control over retry logic and alerting.

```text
n8n workflow:
    │
    ├── HTTP Request to compadre primary (timeout: 120s)
    │   ├── Success → send transcription to WhatsApp
    │   └── Timeout/Error ─┐
    │                       ▼
    │   HTTP Request to compadre backup (timeout: 120s)
    │   ├── Success → send transcription to WhatsApp
    │   └── Timeout/Error → alert sysop
    │
    └── On any failover: notify sysop (Slack/email/webhook)
        "compadre primary overloaded, failover triggered"
```text
**Why n8n-side failover (not Caddy):**

- n8n can alert the sysop about overload
- n8n controls retry logic per conversation context
- Caddy `least_conn` doesn't know about queue depth or overload

**n8n timeout configuration:**

- Primary request: 120 seconds
- Backup request: 120 seconds
- Total worst case: 240 seconds (within n8n's 300s workflow timeout)

### When to use Caddy load balancer vs n8n failover

| Scenario | Approach |
|---|---|
| Predictable high volume (always 30+ conversations) | Caddy load balancer across N instances |
| Normal volume with occasional spikes | n8n failover to backup instance |
| Mixed: high volume + need for alerting | Both: Caddy for primary pool + n8n failover to separate backup |

## Docker Compose

```yaml
services:
  compadre-stt:
    build: .
    expose:
      - "8000"
    deploy:
      replicas: 1
      resources:
        limits:
          cpus: "4"
          memory: 6G
    environment:
      - KEY_REGISTRY_ENCRYPTION_KEY=${KEY_REGISTRY_ENCRYPTION_KEY}
      - KEY_REGISTRY_DB_PATH=/var/lib/compadre/registry.db
      - COMPADRE_WHISPER_MODEL_SIZE=large-v3-turbo
      - COMPADRE_WHISPER_COMPUTE_TYPE=int8
      - COMPADRE_WHISPER_CPU_THREADS=4
      - COMPADRE_WHISPER_LANGUAGE=pt
      - COMPADRE_HF_HOME=/app/models
      - MAX_UPLOAD_SIZE_MB=25
      - MAX_AUDIO_DURATION_SECONDS=600
    volumes:
      - compadre-models:/app/models:rw
      - compadre-registry:/var/lib/compadre:rw
    restart: unless-stopped
```text

Two services: `compadre-stt` (STT API, internal port 8000) + `caddy` (HTTPS reverse proxy, ports 80/443). All external access goes through Caddy. The Caddyfile uses `handle` blocks to route API paths (`/v1/*`, `/health`) to the reverse proxy and everything else to `file_server` for the landing page (served from `/srv/www`). The `reverse_proxy` block includes `trusted_proxies private_ranges` (for reliable `X-Forwarded-For` client IP extraction) and `lb_policy least_conn`. Scale by changing `deploy.replicas` — Caddy load-balances across replicas via the `dynamic a` upstream resolver (re-resolves Docker DNS every 5s). No Redis, no workers.

## File Dependencies

### faster-whisper model

| File / Directory | Source | Size |
|---|---|---|
| `models--Systran--faster-whisper-large-v3-turbo/` | HuggingFace Hub (auto-downloaded on first startup) | ~1.5 GB (int8 quantized) |
| `ffmpeg` | System package | — |
| `ffprobe` | System package | — |

The model auto-downloads from HuggingFace on first startup via the CTranslate2 model hub. For air-gapped deployments, pre-seed the HuggingFace cache directory (`COMPADRE_HF_HOME`) using the `deploy-models.yml` Ansible playbook.
