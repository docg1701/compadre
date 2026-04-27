# API Reference

Compadre exposes an OpenAI-compatible STT endpoint and a health endpoint.

**Last updated:** 2026-04-26

## Authentication

All requests to `/v1/audio/transcriptions` require a Bearer token in the `Authorization` header. The token must be an active, non-expired API key issued via the `compadre-keys` CLI and stored in the encrypted registry.

```text
Authorization: Bearer cmpd_live_<plaintext-issued-once>
```

Keys are issued with `compadre-keys issue-key`. The plaintext is displayed once at issuance and never again — the registry stores only a SHA-256 hash and an 8-character display prefix. See [monetization.md](monetization.md) for the full lifecycle.

The `/health` endpoint does not require authentication. When called without a valid bearer, it returns a minimal payload (`status` and `models`). Passing a valid bearer unlocks the full payload (engines, queue depth, uptime, memory, version).

## POST /v1/audio/transcriptions

Transcribe audio to text.

### Request

**Headers:**

| Header | Required | Value |
| --- | --- | --- |
| `Authorization` | Yes | `Bearer cmpd_live_<plaintext>` |
| `Content-Type` | Yes | `multipart/form-data` |

**Form fields:**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `file` | binary | Yes | — | Audio file (OPUS in OGG, MP3, M4A, WAV, WebM) |
| `model` | string | No | `whisper-large-v3-turbo` | Accepted for OpenAI SDK compatibility; ignored |
| `language` | string | No | `pt` | ISO 639-1 language code; omit for auto-detect |
| `response_format` | string | No | `text` | `text`, `json`, `verbose_json`, `srt`, `vtt` |
| `temperature` | float | No | `0.0` | Accepted for compatibility; ignored |

### Processing Pipeline

```
auth → rate-limit → acquire lock → ffmpeg decode (16 kHz mono PCM s16le WAV)
  → faster-whisper transcribe → format output → return
```

**Engine parameters (non-configurable):**

| Parameter | Value | Rationale |
|---|---|---|
| `beam_size` | `5` | Quality/speed trade-off |
| `word_timestamps` | `True` | Enables `verbose_json`, `srt`, `vtt` formats |
| `condition_on_previous_text` | `False` | Prevents hallucination cascades on long audio |
| `language` | `pt` (default) | Brazilian Portuguese; auto-detect when omitted |
| `cpu_threads` | `4` (env-configurable via `COMPADRE_WHISPER_CPU_THREADS`) | Matches 4 vCPU reference hardware |

**Audio limits:**

| Limit | Value | Enforced by |
|---|---|---|
| Max upload size | 25 MB | FastAPI middleware + Caddy bounded-payload policy |
| Max audio duration | 10 minutes | `ffprobe` validation before engine invocation |

Exceeding either limit returns HTTP 400.

### Success Responses

#### `response_format=text` (default)

**Status:** 200 OK  
**Content-Type:** `text/plain`

```text
Bom dia, como posso ajudar?
```

#### `response_format=json`

**Status:** 200 OK  
**Content-Type:** `application/json`

```json
{
  "text": "Bom dia, como posso ajudar?"
}
```

#### `response_format=verbose_json`

**Status:** 200 OK  
**Content-Type:** `application/json`

```json
{
  "text": "Bom dia, como posso ajudar?",
  "language": "pt",
  "duration": 3.24,
  "segments": [
    {
      "start": 0.0,
      "end": 3.24,
      "text": "Bom dia, como posso ajudar?",
      "words": [
        {"word": "Bom", "start": 0.0, "end": 0.42, "probability": 0.98},
        {"word": "dia", "start": 0.42, "end": 0.78, "probability": 0.97},
        {"word": "como", "start": 1.02, "end": 1.32, "probability": 0.96},
        {"word": "posso", "start": 1.32, "end": 1.68, "probability": 0.95},
        {"word": "ajudar", "start": 1.68, "end": 2.1, "probability": 0.97}
      ]
    }
  ]
}
```

#### `response_format=srt`

**Status:** 200 OK  
**Content-Type:** `text/plain`

```text
1
00:00:00,000 --> 00:00:03,240
Bom dia, como posso ajudar?
```

#### `response_format=vtt`

**Status:** 200 OK  
**Content-Type:** `text/plain`

```text
WEBVTT

00:00:00.000 --> 00:00:03.240
Bom dia, como posso ajudar?
```

### Error Responses

All errors return JSON with this structure:

```json
{
  "error": {
    "message": "description of what went wrong",
    "type": "error_type_identifier",
    "hint": "actionable recovery instructions"
  }
}
```

**400 -- Invalid Request:**

Missing file:

```json
{
  "error": {
    "message": "Missing audio file",
    "type": "invalid_request_error",
    "hint": "Upload an audio file using multipart/form-data with field name 'file'."
  }
}
```

Unsupported format:

```json
{
  "error": {
    "message": "Unsupported audio format: audio/flac",
    "type": "invalid_request_error",
    "hint": "Supported formats: OPUS (OGG), MP3, M4A, WAV, WebM."
  }
}
```

File too large:

```json
{
  "error": {
    "message": "File size exceeds 25 MB limit",
    "type": "invalid_request_error",
    "hint": "Compress the audio or split into shorter segments."
  }
}
```

Audio too long:

```json
{
  "error": {
    "message": "Audio duration exceeds 600 seconds limit",
    "type": "invalid_request_error",
    "hint": "Split the audio into segments under 10 minutes."
  }
}
```

**401 -- Authentication Error:**

Missing or malformed `Authorization` header:

```json
{
  "error": {
    "message": "Missing or malformed Authorization header",
    "type": "authentication_error",
    "hint": "Send 'Authorization: Bearer <plaintext>' using a key issued via compadre-keys issue-key."
  }
}
```

Unknown API key (not present in the registry, or a token that never existed):

```json
{
  "error": {
    "message": "Invalid API key",
    "type": "authentication_error",
    "hint": "The key was not found in the registry. Contact your Compadre operator for a fresh key."
  }
}
```

A 401 is also returned when a key is suspended, revoked, or past `paid_until`. The `hint` field echoes the operator-contact string configured via `OPERATOR_CONTACT_HINT`.

**404 -- Not Found:**

```json
{
  "error": {
    "message": "Not found",
    "type": "not_found_error",
    "hint": "Check the URL. Valid endpoints: POST /v1/audio/transcriptions, GET /health."
  }
}
```

**405 -- Method Not Allowed:**

```json
{
  "error": {
    "message": "Method not allowed",
    "type": "method_not_allowed_error",
    "hint": "Use POST for /v1/audio/transcriptions and GET for /health."
  }
}
```

**429 -- Rate Limit Exceeded:**

```json
{
  "error": {
    "message": "Rate limit exceeded",
    "type": "rate_limit_error",
    "hint": "Too many requests. Try again in 37 seconds."
  }
}
```

The 429 response carries a `Retry-After: <N>` header and matching `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers. `Retry-After` (and the matching hint) are derived from `X-RateLimit-Reset - now`, so clients can back off accurately. The same three `X-RateLimit-*` headers are also emitted on allowed responses. Rate-limiting is per API key, not per IP: each key's `rate_limit_rpm` (set at `compadre-keys issue-key --rpm <n>` and adjustable via `update-key --rpm <n>`) determines the budget. The bucket is keyed on the registry's `key_id`, so it survives `compadre-keys rotate-key` and is independent across two keys issued to the same customer. The STT engine is never invoked when a request hits the rate limit. See [rate-limiting.md](rate-limiting.md) for the algorithm and multi-replica caveat. `/health` has no application-layer rate limit.

**500 -- Server Error:**

```json
{
  "error": {
    "message": "An internal server error occurred",
    "type": "server_error",
    "hint": "Check server logs for the full traceback. The STT model may need restarting."
  }
}
```

**503 -- Service Unavailable:**

```json
{
  "error": {
    "message": "Service is warming up",
    "type": "service_unavailable",
    "hint": "The service is still loading models. Retry in 30 seconds."
  }
}
```

The 503 response includes a `Retry-After` header with the recommended wait time in seconds.

## GET /health

Returns service health status. No authentication required.

### Success Response (200)

```json
{
  "status": "healthy",
  "models": ["large-v3-turbo"],
  "engines": {
    "whisper": {
      "loaded": true,
      "model": "large-v3-turbo",
      "compute_type": "int8",
      "cpu_threads": 4
    }
  },
  "queue_depth": 0,
  "uptime_seconds": 3600,
  "memory_mb": 2100,
  "version": "1.0.0"
}
```

### Warming Up Response (503)

Returned while the model is still loading after startup:

```json
{
  "status": "warming_up",
  "models": [],
  "engines": {},
  "queue_depth": 0,
  "uptime_seconds": 15,
  "memory_mb": 800,
  "version": "1.0.0"
}
```

### Status Values

| Status | HTTP Code | Meaning |
| --- | --- | --- |
| `healthy` | 200 | Model loaded, service ready |
| `warming_up` | 503 | Model still downloading or loading |

## Examples

**Transcription request (text format):**

```bash
curl -k -s https://YOUR_VPS_DOMAIN/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -F "file=@audio.opus" \
  -F "response_format=text"
```

**Transcription request (verbose JSON with timestamps):**

```bash
curl -k -s https://YOUR_VPS_DOMAIN/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -F "file=@audio.opus" \
  -F "response_format=verbose_json"
```

**Transcription request (SRT subtitles):**

```bash
curl -k -s https://YOUR_VPS_DOMAIN/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -F "file=@audio.opus" \
  -F "response_format=srt"
```

**Language override (auto-detect omitted, force Spanish):**

```bash
curl -k -s https://YOUR_VPS_DOMAIN/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -F "file=@audio.opus" \
  -F "language=es" \
  -F "response_format=json"
```

**Missing authentication (error example):**

```bash
curl -k -s https://YOUR_VPS_DOMAIN/v1/audio/transcriptions \
  -F "file=@audio.opus"
```

Response (401):

```json
{
  "error": {
    "message": "Missing or malformed Authorization header",
    "type": "authentication_error",
    "hint": "Send 'Authorization: Bearer <plaintext>' using a key issued via compadre-keys issue-key."
  }
}
```

**Health check:**

```bash
curl -k -s https://YOUR_VPS_DOMAIN/health
```
