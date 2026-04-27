# API Reference

Comadre exposes an OpenAI-compatible TTS endpoint and a health endpoint.

## Authentication

All requests to `/v1/audio/speech` require a Bearer token in the `Authorization` header. The token must be an active, non-expired API key issued via the `compadre-keys` CLI and stored in the encrypted registry.

```text
Authorization: Bearer cmpd_live_<plaintext-issued-once>
```

Keys are issued with `compadre-keys issue-key`. The plaintext is displayed once at issuance and never again — the registry stores only a SHA-256 hash and an 8-character display prefix. See [monetization.md](monetization.md) for the full lifecycle.

The `/health` endpoint does not require authentication. When called without a valid bearer, it returns a minimal payload (`status` and `voices`). Passing a valid bearer unlocks the full payload (engines, queue depth, uptime, memory, version).

## POST /v1/audio/speech

Generate speech audio from text input.

### Request

**Headers:**

| Header | Required | Value |
| --- | --- | --- |
| `Authorization` | Yes | `Bearer cmpd_live_<plaintext>` |
| `Content-Type` | Yes | `application/json` |

**Body:**

| Field | Type | Required | Default | Description |
| --- | --- | --- | --- | --- |
| `input` | string | Yes | -- | Text to synthesize (1-500 chars for Kokoro, 1-300 chars for Qwen3) |
| `voice` | string | No | per `DEFAULT_VOICE` env var (default: `kokoro/pm_santa`) | Voice name in `engine/name` format (see voice routing table below and [voice-management.md](voice-management.md)) |
| `response_format` | string | No | `ogg` | Output format: `ogg` or `wav` |
| `speed` | float | No | `1.0` | Speed multiplier: 0.25 to 4.0. Applied by Kokoro (adjusts phoneme duration during synthesis). Accepted but ignored by Qwen3 (the C++ binary has no speed flag; the whatsapp-subtle pipeline applies a fixed 1.15x atempo independently). |
| `raw` | boolean | No | `false` | Skip audio post-processing (vibrato, EQ, compression, echo, noise mixing). Returns the raw TTS engine output encoded to the requested format. Useful for testing or custom downstream processing. |

### Voice Routing

The `voice` parameter determines which TTS engine processes the request:

| Voice | Engine | Character Limit | Typical Latency (4 vCPU) |
| --- | --- | --- | --- |
| `kokoro/pm_santa` (M) | Kokoro fp32 | 500 | 5-16s |
| `kokoro/pf_dora` (F) | Kokoro fp32 | 500 | 5-16s |
| `kokoro/pm_alex` (M) | Kokoro fp32 | 500 | 5-16s |
| `kokoro/<name>` | Kokoro fp32 | 500 | 5-16s |
| `qwen/<name>` | Qwen3-TTS C++ Q8_0 | 300 | 12-37s |

Voices are discovered from the filesystem. See [voice-management.md](voice-management.md) for how to add or list voices.

### Engine-Specific Preprocessing

Each engine applies different text preprocessing before synthesis:

**Kokoro:**

- `normalize_text()` -- normalizes Unicode, expands abbreviations
- `chunk_text()` -- splits long text into chunks for sequential synthesis
- espeak-ng pt_extra dictionary -- handles sigla pronunciation (CPF, CNPJ, FGTS, etc.) via 67 custom dictionary entries

**Qwen3:**

- `expand_siglas()` -- converts siglas to spaced letter format (e.g., `CNPJ` becomes `C N P J`) for correct pronunciation

### Post-Processing Pipelines

Each engine has a dedicated audio post-processing pipeline applied before output:

**radio_warm (Kokoro):** EQ shaping (bass boost, mid-scoop, presence), dynamic compression, saturation, loudnorm, room/hiss noise layer.

**whatsapp-subtle (Qwen3):** EQ shaping, dynamic compression, atempo 1.15x speed adjustment, echo, room/hiss noise layer.

Both pipelines produce the final audio optimized for messaging platform voice notes.

### Success Response

**Status:** 200 OK

**Headers:**

- `Content-Type: audio/ogg` (for OGG Opus) or `Content-Type: audio/wav` (for WAV)

**Body:** Binary audio data.

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

```json
{
  "error": {
    "message": "Input exceeds 300 character limit for Qwen3 engine (voice 'qwen/galvani')",
    "type": "invalid_request_error",
    "hint": "Reduce text length or switch to a Kokoro voice (kokoro/pm_santa, kokoro/pf_dora, kokoro/pm_alex) which supports up to 500 characters."
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
    "hint": "The key was not found in the registry. Contact your Comadre operator for a fresh key."
  }
}
```

A 401 is also returned when a key is suspended, revoked, or past `paid_until`. The `hint` field echoes the operator-contact string configured via `OPERATOR_CONTACT_HINT`.

**404 -- Voice Not Found:**

```json
{
  "error": {
    "message": "Voice 'unknown' not found",
    "type": "not_found_error",
    "hint": "Available voices: ['kokoro/pf_dora', 'kokoro/pm_alex', 'kokoro/pm_santa', 'qwen/female', 'qwen/galvani']. Use engine/name format. See /health for the current list."
  }
}
```

**405 -- Method Not Allowed:**

```json
{
  "error": {
    "message": "Method not allowed",
    "type": "method_not_allowed_error",
    "hint": "Use POST for /v1/audio/speech and GET for /health."
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

The 429 response carries a `Retry-After: <N>` header and matching `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset` headers. `Retry-After` (and the matching hint) are derived from `X-RateLimit-Reset - now`, so clients can back off accurately. The same three `X-RateLimit-*` headers are also emitted on allowed responses. Rate-limiting is per API key, not per IP: each key's `rate_limit_rpm` (set at `compadre-keys issue-key --rpm <n>` and adjustable via `update-key --rpm <n>`) determines the budget. The bucket is keyed on the registry's `key_id`, so it survives `compadre-keys rotate-key` and is independent across two keys issued to the same customer. The TTS engine is never invoked when a request hits the rate limit. See [rate-limiting.md](rate-limiting.md) for the algorithm and multi-replica caveat. `/health` has no application-layer rate limit.

**500 -- Server Error:**

```json
{
  "error": {
    "message": "An internal server error occurred",
    "type": "server_error",
    "hint": "Check server logs for the full traceback. The TTS model may need restarting."
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
  "voices": ["kokoro/pf_dora", "kokoro/pm_alex", "kokoro/pm_santa", "qwen/female", "qwen/galvani"],
  "engines": {
    "kokoro": {"loaded": true, "voices": ["kokoro/pf_dora", "kokoro/pm_alex", "kokoro/pm_santa"]},
    "qwen3": {"loaded": true, "voices": ["qwen/female", "qwen/galvani"]}
  },
  "queue_depth": 0,
  "uptime_seconds": 3600,
  "memory_mb": 4200,
  "version": "2.0.0"
}
```

### Warming Up Response (503)

Returned while models are still loading after startup:

```json
{
  "status": "warming_up",
  "voices": [],
  "engines": {},
  "queue_depth": 0,
  "uptime_seconds": 15,
  "memory_mb": 800,
  "version": "2.0.0"
}
```

### Status Values

| Status | HTTP Code | Meaning |
| --- | --- | --- |
| `healthy` | 200 | All engines loaded, service ready |
| `warming_up` | 503 | Models still downloading or loading |

## Voice Cloning

Qwen3-TTS voices use reference WAV files for voice cloning. Voices are discovered from the `/voices/qwen/` directory -- no environment variable configuration needed. For detailed setup instructions, see [voice-management.md](voice-management.md).

**How it works:**

1. Place a reference WAV (3-10s of clear speech) in `/voices/qwen/`
2. The voice is immediately available as `qwen/<filename-without-extension>`
3. No restart needed -- voices are discovered at request time

**Seed reproducibility:** Qwen3-TTS uses a fixed seed for consistent voice characteristics. The same text + reference WAV + seed produces similar results across runs.

**No Qwen3 voices:** If no WAV files are present in `/voices/qwen/`, no Qwen3 voices will appear in the `/health` endpoint. Requests for missing voices return a 404 error.

## Examples

**Kokoro request (fast, built-in voice):**

```bash
curl -X POST https://YOUR_VPS_DOMAIN/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -H "Content-Type: application/json" \
  -d '{"input":"Bom dia, como posso ajudar?", "voice":"kokoro/pm_santa"}' \
  --output test.ogg
```

**Qwen3 request (cloned voice):**

```bash
curl -X POST https://YOUR_VPS_DOMAIN/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -H "Content-Type: application/json" \
  -d '{"input":"Seu CPF foi validado com sucesso.", "voice":"qwen/galvani"}' \
  --output test_clone.ogg
```

**Missing authentication (error example):**

```bash
curl -X POST https://YOUR_VPS_DOMAIN/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"Teste sem autenticacao."}'
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
curl https://YOUR_VPS_DOMAIN/health
```
