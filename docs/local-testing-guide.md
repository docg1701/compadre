# Local Testing Guide

How to run Comadre end-to-end on your machine with all features working.

## Prerequisites

- Docker and Docker Compose v2
- 4 vCPU, 8 GB RAM minimum
- ~4 GB free disk (models + Docker image)
- No GPU required (CPU-only inference)

## What is already in the repo

These files are tracked in git and require no action:

| Item | Location | Purpose |
| --- | --- | --- |
| `qwen3-tts-cli` binary | `deploy/bin/` | Qwen3 C++ inference CLI |
| Voice reference WAVs | `voices/` | 3 files: female, male, galvani |
| Noise WAVs | `noise/` | 2 files: hiss + room tone for post-processing |
| espeak-ng pt_extra dictionary | `deploy/espeak-ng/pt_extra` | Custom sigla pronunciation (CPF, CNPJ, etc.) |

## What must be obtained separately

These files are gitignored because of their size:

| Item | Size | How to get |
| --- | --- | --- |
| `kokoro-v1.0.onnx` | 325 MB | Auto-downloads on first startup |
| `voices-v1.0.bin` | 27 MB | Auto-downloads on first startup |
| `qwen3-tts-0.6b-q8_0.gguf` | 1.3 GB | Manual conversion from HuggingFace |
| `qwen3-tts-tokenizer-f16.gguf` | 326 MB | Manual conversion from HuggingFace |

Kokoro models download automatically. Qwen3 models must be converted or copied manually (see below).

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

## Step 2: Obtain Qwen3 models

Skip this step if you only want to test Kokoro voices (`kokoro/pm_santa`, `kokoro/pf_dora`, `kokoro/pm_alex`). Qwen3 voices (`qwen/<name>`) require the GGUF models.

### Option A: Copy existing models (if you already have them)

If you previously converted the models or have them from another machine:

```bash
# Place them in the models/ directory
cp /path/to/qwen3-tts-0.6b-q8_0.gguf models/
cp /path/to/qwen3-tts-tokenizer-f16.gguf models/
```

### Option B: Convert from HuggingFace (fresh machine)

This is a one-time process that takes about 20 minutes and needs ~5 GB of temporary disk space. Run these commands in a separate terminal (not inside Claude Code, which has shell timeouts).

```bash
# 1. Create a temporary Python venv for conversion (CPU-only PyTorch)
uv venv tmp/convert-venv

# 2. Install conversion dependencies
uv pip install --python tmp/convert-venv/bin/python \
  torch --index-url https://download.pytorch.org/whl/cpu
uv pip install --python tmp/convert-venv/bin/python \
  numpy safetensors tqdm gguf huggingface_hub

# 3. Download the HuggingFace model (~1.2 GB)
tmp/convert-venv/bin/hf download Qwen/Qwen3-TTS-12Hz-0.6B-Base \
  --local-dir tmp/Qwen3-TTS-12Hz-0.6B-Base

# 4. Clone the conversion scripts
git clone --depth 1 https://github.com/predict-woo/qwen3-tts.cpp.git tmp/qwen3-tts-convert

# 5. Convert TTS model to Q8_0
mkdir -p models
tmp/convert-venv/bin/python tmp/qwen3-tts-convert/scripts/convert_tts_to_gguf.py \
  --input tmp/Qwen3-TTS-12Hz-0.6B-Base \
  --output models/qwen3-tts-0.6b-q8_0.gguf \
  --type q8_0

# 6. Convert tokenizer to F16
tmp/convert-venv/bin/python tmp/qwen3-tts-convert/scripts/convert_tokenizer_to_gguf.py \
  --input tmp/Qwen3-TTS-12Hz-0.6B-Base \
  --output models/qwen3-tts-tokenizer-f16.gguf \
  --type f16

# 7. Verify the files
ls -lh models/qwen3-tts-0.6b-q8_0.gguf     # ~1.3 GB
ls -lh models/qwen3-tts-tokenizer-f16.gguf  # ~326 MB

# 8. Clean up temporary files (~5 GB freed)
rm -rf tmp/convert-venv tmp/Qwen3-TTS-12Hz-0.6B-Base tmp/qwen3-tts-convert
```

For the full conversion reference, see [quantizing-qwen3-models.md](quantizing-qwen3-models.md).

## Step 3: Load Qwen3 models into Docker volume

The `models/` directory is not bind-mounted. Docker Compose uses a named volume (`compadre-models`). Copy the models into it:

```bash
docker volume create compadre-models
docker run --rm \
  -v compadre-models:/models \
  -v "$(pwd)/models":/src:ro \
  alpine sh -c 'cp /src/qwen3-tts-0.6b-q8_0.gguf /src/qwen3-tts-tokenizer-f16.gguf /models/'
```

Kokoro models do not need this step -- they auto-download into the volume on first startup.

## Step 4: Build and start

```bash
docker compose up --build -d
```

First build takes 3-5 minutes (espeak-ng compilation, Python dependencies). Subsequent builds use Docker cache.

## Step 5: Wait for startup

Watch the logs until you see the service is ready:

```bash
docker compose logs -f
```

Startup phases:

1. Kokoro models download (if not cached) -- 1-2 minutes
2. Qwen3 model loading -- 10-20 seconds
3. Engine warm-up (dummy inference) -- 10-30 seconds
4. `startup | Service ready` appears in logs

Check the health endpoint:

```bash
curl -sk https://localhost/health | python3 -m json.tool
```

Expected response when fully ready:

```json
{
    "status": "healthy",
    "voices": ["kokoro/pf_dora", "kokoro/pm_alex", "kokoro/pm_santa", "qwen/female", "qwen/galvani"],
    "engines": {
        "kokoro": {"loaded": true, "voices": ["kokoro/pf_dora", "kokoro/pm_alex", "kokoro/pm_santa"]},
        "qwen3": {"loaded": true, "voices": ["qwen/female", "qwen/galvani"]}
    },
    "queue_depth": 0,
    "uptime_seconds": 42,
    "memory_mb": 1024,
    "version": "2.0.0"
}
```

If you skipped Qwen3 models, `qwen3.loaded` will be `false` and only Kokoro voices will appear.

## Step 6: Test each voice

### Kokoro voices (5-16 seconds latency)

```bash
# kokoro/pm_santa (male, default)
curl -k -X POST https://localhost/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Bom dia, como posso ajudar?", "voice":"kokoro/pm_santa"}' \
  --output test-santa.ogg

# kokoro/pf_dora (female)
curl -k -X POST https://localhost/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Seu CPF foi validado com sucesso.", "voice":"kokoro/pf_dora"}' \
  --output test-dora.ogg

# kokoro/pm_alex (male)
curl -k -X POST https://localhost/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"O FGTS foi depositado na sua conta.", "voice":"kokoro/pm_alex"}' \
  --output test-alex.ogg
```

### Qwen3 voices (12-37 seconds latency)

```bash
# qwen/female
curl -k -X POST https://localhost/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Obrigada por entrar em contato.", "voice":"qwen/female"}' \
  --output test-qwen-female.ogg

# qwen/male
curl -k -X POST https://localhost/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Seu atendimento foi registrado.", "voice":"qwen/male"}' \
  --output test-qwen-male.ogg

# qwen/galvani (uses galvani.wav reference)
curl -k -X POST https://localhost/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Esta e a voz clonada falando.", "voice":"qwen/galvani"}' \
  --output test-galvani.ogg
```

### WAV output (instead of OGG Opus)

```bash
curl -k -X POST https://localhost/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Teste em formato WAV.", "voice":"kokoro/pm_santa", "response_format":"wav"}' \
  --output test.wav
```

### Live playback (pipe to ffplay)

Stream audio directly to speakers without saving a file. Requires WAV format for pipe compatibility:

```bash
# Kokoro female voice
curl -s -X POST https://YOUR_VPS_DOMAIN/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Seu CPF foi validado com sucesso.","voice":"kokoro/pf_dora","response_format":"wav"}' \
  | ffplay -nodisp -autoexit -

# Qwen3 cloned voice
curl -s -X POST https://YOUR_VPS_DOMAIN/v1/audio/speech \
  -H "Authorization: Bearer cmpd_live_YOUR_LOCAL_KEY" \
  -H "Content-Type: application/json" \
  -d '{"input":"Seu CPF foi validado com sucesso.","voice":"qwen/female","response_format":"wav"}' \
  | ffplay -nodisp -autoexit -
```

OGG Opus does not support pipe streaming. Use `response_format: wav` when piping to ffplay.

### Error example (missing auth)

```bash
curl -k -X POST https://localhost/v1/audio/speech \
  -H "Content-Type: application/json" \
  -d '{"input":"Teste sem autenticacao."}'
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

## Stopping and restarting

```bash
# Stop
docker compose down

# Restart (models persist in Docker volumes)
docker compose up -d

# Full reset (removes all volumes, models must be re-downloaded/copied)
docker compose down -v
```

## Troubleshooting

### Health returns 503 warming_up indefinitely

Check logs for download or loading errors:

```bash
docker compose logs --tail=50
```

Common causes: network issue during Kokoro download, missing Qwen3 GGUF files, insufficient RAM.

### Qwen3 voices not available

Verify models are in the volume:

```bash
docker run --rm -v compadre-models:/models alpine ls -lh /models/
```

Expected: `qwen3-tts-0.6b-q8_0.gguf` (~1.3 GB) and `qwen3-tts-tokenizer-f16.gguf` (~326 MB).

### Siglas pronounced incorrectly

Rebuild the image to recompile espeak-ng pt_extra:

```bash
docker compose up -d --build --force-recreate
```

### Port already in use

Change the port in `.env`:

```bash
PORT=9000
```

Then restart: `docker compose up -d`

For more issues, see [troubleshooting.md](troubleshooting.md).
