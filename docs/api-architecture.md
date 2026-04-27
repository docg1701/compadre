# Comadre API & Architecture Specification

**Date:** 2026-03-23
**Status:** Definitive specification for implementation
**Last updated:** 2026-03-23

## Overview

Comadre is a self-hosted pt-BR TTS API for WhatsApp automation via n8n. Dual-engine: Kokoro (fast, no cloning) and Qwen3-TTS C++ (voice cloning). Single FIFO queue per instance, horizontal scaling via Caddy load balancer.

## API Endpoint

### `POST /v1/audio/speech`

OpenAI-compatible endpoint. n8n sends text, receives audio.

#### Request

```json
{
  "input": "O INSS confirmou que o seu benefício BPC LOAS foi concedido.",
  "voice": "kokoro/pm_santa",
  "response_format": "ogg",
  "speed": 1.0,
  "raw": false
}
```

#### Parameters

| Parameter | Type | Required | Default | Values | Description |
|---|---|---|---|---|---|
| `input` | string | Yes | — | max 500 chars (Kokoro) / 300 chars (Qwen3) | Text to synthesize in pt-BR |
| `voice` | string | No | — | `kokoro/pm_santa`, `kokoro/pf_dora`, `kokoro/pm_alex`, `qwen/<name>` | Voice selection (engine/name format, see [voice-management.md](voice-management.md)) |
| `response_format` | string | No | `"ogg"` | `ogg`, `wav` | Output format |
| `speed` | float | No | `1.0` | 0.25 — 4.0 | Speed multiplier. Applied by Kokoro (adjusts phoneme duration). Accepted but ignored by Qwen3. |
| `raw` | boolean | No | `false` | true/false | Skip audio post-processing (vibrato, EQ, compression, noise) |

#### Engine routing (based on `voice` prefix)

Voice names use the `engine/name` format. The engine prefix determines the TTS backend. Voices are discovered from the filesystem at runtime -- see [voice-management.md](voice-management.md) for details.

| Voice pattern | Engine | Behavior |
|---|---|---|
| `kokoro/pm_santa` | Kokoro fp32 | Fast (RTF 1.1x), perfect siglas via espeak-ng pt_extra |
| `kokoro/pf_dora` | Kokoro fp32 | Female voice, fast |
| `kokoro/pm_alex` | Kokoro fp32 | Male alternative, fast |
| `kokoro/<name>` | Kokoro fp32 | Any built-in or custom .npy voice profile |
| `qwen/<name>` | Qwen3 C++ Q8_0 | Voice cloning from reference WAV in `/voices/qwen/` (RTF 2.5x) |

#### Preprocessing (automatic, not configurable)

**Kokoro path:**

- `normalize_text()`: numbers, currency (R$), dates (dd/mm/yyyy), times (HH:MM) → pt-BR words
- `chunk_text()`: split at sentence boundaries, max 200 tokens per chunk
- Siglas handled natively by espeak-ng custom `pt_extra` dictionary (67 entries)

**Qwen3 path:**

- `expand_siglas()`: CNPJ → `C N P J`, INSS → `I N S S` (spaced format for spelled siglas)
- Word acronyms kept as-is: SUS, PIX, PIS, LOAS, CEP, DETRAN, ENEM, SAMU, ANVISA
- Siglas must be embedded between normal words (never start sentence with spelled sigla)

#### Post-processing (`raw=false`)

**Kokoro pipeline (radio_warm):**

```text
highpass(100Hz) → bass(+5dB@150Hz) →
EQ(+3dB@250Hz, -4dB@600Hz, +5dB@3.5kHz) →
compressor(6:1, -24dB, makeup=2) → softclip(tanh, 0.3) →
lowpass(7kHz) → loudnorm(-16 LUFS) →
noise(room=0.45, hiss=0.24) → loudnorm(-17 LUFS) →
OGG Opus 24kbps 16kHz mono
```

**Qwen3 pipeline (whatsapp-subtle):**

```text
highpass(100Hz) → EQ(+1.5dB@200Hz, -2dB@700Hz, +3dB@3kHz, -4dB@7kHz) →
compressor(6:1) → lowpass(8kHz) → echo(40ms, 0.12) →
atempo(1.15x) → loudnorm(-18 LUFS) →
noise(room=0.15, hiss=0.08) → loudnorm(-17 LUFS) →
OGG Opus 24kbps 16kHz mono
```

**`speed` parameter:** Applied by Kokoro -- `kokoro_onnx.create(speed=...)` adjusts phoneme duration during synthesis. Accepted but ignored by Qwen3 (the C++ binary has no speed flag). When `raw=false` (default), Qwen3's whatsapp-subtle pipeline applies a fixed 1.15x atempo adjustment independently of the `speed` field.

#### Response

```text
HTTP 200
Content-Type: audio/ogg (or audio/wav)
Body: [audio bytes]
```

#### Errors

| Code | Condition |
|---|---|
| 400 | `input` empty or exceeds character limit |
| 400 | Malformed request or voice format |
| 401 | Invalid bearer token, or key suspended/revoked/past-due in the registry |
| 404 | Voice not found |
| 405 | Method not allowed |
| 429 | Rate limit exceeded |
| 500 | TTS generation failed |
| 503 | Engine not loaded |

### `GET /health`

```json
{
  "status": "healthy",
  "voices": ["kokoro/pf_dora", "kokoro/pm_alex", "kokoro/pm_santa", "qwen/female", "qwen/galvani"],
  "engines": {
    "kokoro": {"loaded": true, "voices": ["kokoro/pf_dora", "kokoro/pm_alex", "kokoro/pm_santa"]},
    "qwen3": {"loaded": true, "voices": ["qwen/female", "qwen/galvani"]}
  },
  "queue_depth": 3,
  "uptime_seconds": 3600,
  "memory_mb": 4200,
  "version": "2.0.0"
}
```

### Authentication

```text
Authorization: Bearer cmpd_live_<plaintext>
```

Keys are stored in an encrypted SQLite registry and issued via the `compadre-keys` CLI. Each key carries its own `rate_limit_rpm` and billing metadata. The registry is the sole source of truth — there is no single-key environment-variable fallback. See [monetization.md](monetization.md) and [rate-limiting.md](rate-limiting.md).

## Queue Architecture

### Design: Single FIFO queue per instance

```python
lock = asyncio.Lock()

@app.post("/v1/audio/speech")
async def speech(request: SpeechRequest):
    async with lock:  # One generation at a time, FIFO order
        audio = await run_in_executor(generate_audio, request)
    return Response(content=audio, media_type="audio/ogg")
```

- No Redis, no Celery, no external queue
- `asyncio.Lock` ensures FIFO ordering
- FastAPI keeps HTTP connection open until audio is ready
- n8n waits for response (timeout configurable, default 300s)

### Why this is sufficient

- WhatsApp conversations are async — users don't expect instant responses
- Kokoro generates 5s audio in 5.5s — fast enough for sequential processing
- The queue depth is visible via `/health` endpoint for monitoring
- No complex infrastructure to maintain

### Capacity per VPS (4 CPU, 8 GB RAM)

| Engine | Time per request | Throughput | Max comfortable conversations |
|---|---|---|---|
| Kokoro | ~5.5s | ~10 req/min | ~15-20 |
| Qwen3 | ~12.5s | ~4.8 req/min | ~5-8 |

### Realistic queue behavior

Requests don't arrive simultaneously — they're spread across time because WhatsApp conversations have natural pauses (user reading, typing, thinking). Each conversation generates ~1 request every 30-60 seconds.

**Example: 15 conversations with Kokoro**

- Average request arrival: 1 every 2-4 seconds
- Kokoro processes 1 every 5.5 seconds
- Queue depth oscillates between 0-3 (rarely peaks above 4)
- Typical latency: 5.5s (no queue) to 16s (2-3 in queue)
- Burst scenario (rare): 5+ simultaneous → 27s max wait

**Example: 8 conversations with Qwen3**

- Average request arrival: 1 every 4-7 seconds
- Qwen3 processes 1 every 12.5 seconds
- Queue depth oscillates between 0-2
- Typical latency: 12.5s to 37s
- Burst scenario (rare): 4+ simultaneous → 50s max wait

### Theoretical worst-case (all requests arrive at once)

| Queue depth | Kokoro wait | Qwen3 wait |
|---|---|---|
| 1 | 5.5s | 12.5s |
| 5 | 27.5s | 62.5s |
| 10 | 55s | 125s |

These worst-case numbers rarely occur in practice due to the natural spacing of WhatsApp conversations.

## Horizontal Scaling

When a single instance isn't enough, add more VPS instances behind Caddy.

### Architecture

```text
n8n (any server)
    │
    │ POST /v1/audio/speech
    ▼
Caddy (reverse proxy + load balancer)
    │
    ├── VPS 1: comadre instance (own FIFO queue)
    ├── VPS 2: comadre instance (own FIFO queue)
    └── VPS N: comadre instance (own FIFO queue)
```

### Caddy configuration

```caddyfile
tts.example.com {
    reverse_proxy {
        to vps1.internal:8000 vps2.internal:8000
        lb_policy least_conn
        health_uri /health
        health_interval 30s
    }
}
```

### Scaling rules

| Conversations | Kokoro instances | Qwen3 instances |
|---|---|---|
| < 15 | 1 | 1 |
| 15-30 | 2 | 2-3 |
| 30-60 | 3-4 | 4-6 |
| 60+ | Auto-scale | Auto-scale |

Each instance is independent — own model loaded, own queue, own processing. No shared state. Adding/removing instances requires only Caddy config update.

### Failover pattern (n8n-side)

When a comadre instance times out, the n8n workflow handles failover — not the load balancer. This gives the workflow control over retry logic and alerting.

```text
n8n workflow:
    │
    ├── HTTP Request to comadre primary (timeout: 120s)
    │   ├── Success → send audio to WhatsApp
    │   └── Timeout/Error ─┐
    │                       ▼
    │   HTTP Request to comadre backup (timeout: 120s)
    │   ├── Success → send audio to WhatsApp
    │   └── Timeout/Error → send text message instead + alert sysop
    │
    └── On any failover: notify sysop (Slack/email/webhook)
        "comadre primary overloaded, failover triggered"
```

**Why n8n-side failover (not Caddy):**

- n8n can alert the sysop about overload
- n8n can fall back to text message if both instances fail
- n8n controls retry logic per conversation context
- Caddy least_conn doesn't know about queue depth or overload

**n8n timeout configuration:**

- Primary request: 120 seconds (enough for ~20 in queue with Kokoro)
- Backup request: 120 seconds
- Total worst case: 240 seconds (within n8n's 300s workflow timeout)

### When to use Caddy load balancer vs n8n failover

| Scenario | Approach |
|---|---|
| Predictable high volume (always 30+ conversations) | Caddy load balancer across N instances |
| Normal volume with occasional spikes | n8n failover to backup instance |
| Mixed: high volume + need for alerting | Both: Caddy for primary pool + n8n failover to separate backup |

## Qwen3 Voice Configuration

All Qwen3 voices use x-vector cloning from reference WAV files. The 0.6B-Base model has no built-in voice presets — voice identity is determined entirely by the reference audio's speaker embedding. Voices are discovered from the `/voices/qwen/` directory at runtime.

For detailed voice setup instructions, see [voice-management.md](voice-management.md).

### Reference voices (ship with the project)

| Voice | File | Source | License |
|---|---|---|---|
| `qwen/female` | `/voices/qwen/female.wav` | LibriVox pt-BR — Leni (Contos Brasileiros, track 02) | Public domain |
| `qwen/male` | `/voices/qwen/male.wav` | LibriVox pt-BR — Lucas M. (Contos Brasileiros, track 04) | Public domain |
| `qwen/galvani` | `/voices/qwen/galvani.wav` | Original recording — Galvani (project owner) | Private / owner consent |

### Adding a custom voice

1. Place a reference WAV in `/voices/qwen/` (3-10s, clean speech, mono, 16kHz or higher)
2. The voice is immediately available as `qwen/<filename-without-extension>`
3. No restart or environment variable configuration needed

### Consistency

- Same reference audio + same seed = same voice characteristics
- The seed can be set per request (`seed` parameter) or left random (`-1`)
- For consistent bot personality, the n8n workflow should send the same `seed` value for all messages in a conversation

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
      - DEFAULT_VOICE=kokoro/pm_santa
      - VOICES_DIR=/voices
      - ESPEAK_DATA_PATH=/usr/local/share/espeak-ng-data
    volumes:
      - compadre-voices:/voices:ro
      - compadre-registry:/var/lib/compadre:rw
    restart: unless-stopped
```

Two services: compadre-stt (TTS API, internal port 8000) + caddy (HTTPS reverse proxy, ports 80/443). All external access goes through Caddy. The Caddyfile uses `handle` blocks to route API paths (`/v1/*`, `/health`, `/docs`, `/openapi.json`, `/redoc`) to the reverse proxy and everything else to `file_server` for the landing page (served from `/srv/www`). The `reverse_proxy` block includes `trusted_proxies private_ranges` (for reliable `X-Forwarded-For` client IP extraction) and `lb_policy least_conn`. Scale by changing `replicas` -- Caddy load-balances across replicas via the `dynamic a` upstream resolver (re-resolves Docker DNS every 5s). No Redis, no workers.

## Character Limits

| Engine | Max chars | Rationale |
|---|---|---|
| Kokoro | 500 | Chunked internally at 200 tokens; 500 chars ≈ 2-3 chunks |
| Qwen3 | 300 | No chunking; longer text = longer generation + risk of runaway |

Exceeding the limit returns HTTP 400 with error message.

## File Dependencies

### Kokoro

| File | Source | Size |
|---|---|---|
| `kokoro-v1.0.onnx` | thewh1teagle/kokoro-onnx releases | 325 MB |
| `voices-v1.0.bin` | thewh1teagle/kokoro-onnx releases | 27 MB |
| `pt_dict` + phoneme files | Compiled espeak-ng with pt_extra | 141 KB + ~660 KB |
| `pt_extra` | `deploy/espeak-ng/pt_extra` in this repo | 3 KB |

### Qwen3

| File | Source | Size |
|---|---|---|
| `qwen3-tts-0.6b-q8_0.gguf` | Converted via qwen3-tts.cpp scripts | 1.3 GB |
| `qwen3-tts-tokenizer-f16.gguf` | Converted via qwen3-tts.cpp scripts | 341 MB |
| `qwen3-tts-cli` | Built from predict-woo/qwen3-tts.cpp + seed patch | ~600 KB |

### Post-processing

| File | Source | Size |
|---|---|---|
| `room.wav` | Generated via ffmpeg (anoisesrc pink) | ~1 MB |
| `hiss.wav` | Generated via ffmpeg (anoisesrc white) | ~1 MB |
| `ffmpeg` | System package | — |
