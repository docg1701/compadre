# Local Testing Guide

How to run Compadre end-to-end on your machine.

**Last updated:** 2026-04-26

## Prerequisites

- Docker and Docker Compose v2
- 4 vCPU, 8 GB RAM minimum
- ~3 GB free disk (model + Docker image)
- No GPU required (CPU-only inference)

## What is already in the repo

The faster-whisper model auto-downloads on first startup from HuggingFace Hub. No manual download is required for testing.

## Step 1: Create .env

```bash
cp .env.example .env
```

Edit `.env` and set `KEY_REGISTRY_ENCRYPTION_KEY` — the only required variable. Generate a Fernet key:

```bash
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste the output into `.env`:

```bash
KEY_REGISTRY_ENCRYPTION_KEY=<paste-the-generated-key-here>
```

All other variables have sensible defaults. After `docker compose up -d`, seed a customer and issue a test key:

```bash
docker compose exec compadre-stt compadre-keys create-customer \
  --name "Local Dev" --email dev@localhost --tax-id 11144477735
docker compose exec compadre-stt compadre-keys issue-key \
  --customer <cust_id-from-previous-step> --rpm 1000 --billing-mode complimentary
# The printed cmpd_live_... plaintext is what you pass in curl.
```

## Step 2: Build and start

```bash
docker compose up --build -d
```

First build takes 3-5 minutes (Python dependencies, ffmpeg). Subsequent builds use Docker cache.

## Step 3: Wait for startup

Watch the logs until you see the service is ready:

```bash
docker compose logs -f
```

Startup phases:

1. faster-whisper model download (if not cached) -- 1-2 minutes
2. Engine warm-up -- 10-30 seconds
3. `startup | Service ready` appears in logs

Check the health endpoint:

```bash
curl -sk https://localhost/health | python3 -m json.tool
```

Expected response when fully ready:

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
    "uptime_seconds": 42,
    "memory_mb": 1024,
    "version": "1.0.0"
}
```

## Step 4: Test transcription with all formats

Create a short test audio file (or use an existing one):

```bash
# Generate a 5-second test audio with ffmpeg (optional)
ffmpeg -f lavfi -i "sine=frequency=1000:duration=5" -c:a libopus test.opus
```

### Text format (default)

```bash
curl -k -s https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -F "file=@test.opus" \
  -F "response_format=text"
```

### JSON format

```bash
curl -k -s https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -F "file=@test.opus" \
  -F "response_format=json"
```

### Verbose JSON (with timestamps)

```bash
curl -k -s https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -F "file=@test.opus" \
  -F "response_format=verbose_json"
```

### SRT subtitles

```bash
curl -k -s https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -F "file=@test.opus" \
  -F "response_format=srt"
```

### VTT subtitles

```bash
curl -k -s https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -F "file=@test.opus" \
  -F "response_format=vtt"
```

## Step 5: Test language override and auto-detect

### Force Portuguese (explicit)

```bash
curl -k -s https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -F "file=@test.opus" \
  -F "language=pt" \
  -F "response_format=json"
```

### Auto-detect (pass empty language)

```bash
curl -k -s https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -F "file=@test.opus" \
  -F "language=" \
  -F "response_format=json"
```

## Step 6: Test error cases

### Missing authentication

```bash
curl -k -s https://localhost/v1/audio/transcriptions \
  -F "file=@test.opus"
```

Expected 401 response:

```json
{
  "error": {
    "message": "Missing or malformed Authorization header",
    "type": "authentication_error",
    "hint": "Send 'Authorization: Bearer <plaintext>' using a key issued via compadre-keys issue-key."
  }
}
```

### Oversized upload

Create a large dummy file:

```bash
dd if=/dev/zero of=large.bin bs=1M count=30
curl -k -s https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -F "file=@large.bin"
```

Expected 400 response:

```json
{
  "error": {
    "message": "File size exceeds 25 MB limit",
    "type": "invalid_request_error",
    "hint": "Compress the audio or split into shorter segments."
  }
}
```

## Stopping and restarting

```bash
# Stop
docker compose down

# Restart (model persists in Docker volume)
docker compose up -d

# Full reset (removes all volumes, model must be re-downloaded)
docker compose down -v
```

## Troubleshooting

### Health returns 503 warming_up indefinitely

Check logs for download or loading errors:

```bash
docker compose logs --tail=50
```

Common causes: network issue during model download, insufficient RAM, HuggingFace Hub unreachable.

### ffmpeg not found

Verify ffmpeg is installed in the container:

```bash
docker compose exec compadre-stt ffmpeg -version | head -1
```

If missing, rebuild the image: `docker compose up -d --build`

### Port already in use

Change the port in `.env`:

```bash
PORT=9000
```

Then restart: `docker compose up -d`

For more issues, see [troubleshooting.md](troubleshooting.md).
