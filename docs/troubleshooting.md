# Troubleshooting

Common issues with symptoms, causes, and resolution steps for both engines.

## Startup Failures

### KEY_REGISTRY_ENCRYPTION_KEY not set or malformed

**Symptom:** Container exits immediately on startup. Logs mention `pydantic` validation failure on `key_registry_encryption_key` or "must be a valid Fernet key".

**Cause:** `KEY_REGISTRY_ENCRYPTION_KEY` is missing from `.env`, empty, or not a valid 44-character base64 Fernet key.

**Resolution:**

1. Check `.env` contains a valid key:

   ```bash
   grep KEY_REGISTRY_ENCRYPTION_KEY ~/compadre/.env
   ```

2. If missing, generate one and paste it into `.env`:

   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

3. IMPORTANT: store the key in your password manager. Losing it makes every encrypted tax ID in the registry unrecoverable.
4. Restart: `docker compose up -d`.

### Model download fails

**Symptom:** Container starts but health endpoint returns 503 indefinitely. Logs show download errors.

**Cause:** Network issue, SSL certificate problem, or invalid download URL.

**Resolution:**

1. Check logs: `docker compose logs --tail=50`
2. Look for SSL or connection errors in the download output
3. Verify the download URLs are accessible from the VPS: `curl -I <model-url>`
4. Alternative: pre-deploy models via `deploy-models.yml` playbook and clear download URLs

### ffmpeg/ffprobe not found

**Symptom:** All transcription requests return 500. Logs show `ffmpeg not found` or `ffprobe not found`.

**Cause:** The `ffmpeg` and/or `ffprobe` binaries are not installed in the container.

**Resolution:**

1. Verify ffmpeg is available inside the container:
   ```bash
   docker compose exec compadre-stt ffmpeg -version | head -1
   docker compose exec compadre-stt ffprobe -version | head -1
   ```
2. Check the Dockerfile installs ffmpeg via apt: `apt-get install -y ffmpeg`
3. Rebuild the image: `docker compose up -d --build`

## Runtime Issues

> **Note:** Health check examples below use `localhost`, but Caddy only responds to the Host header matching its configured domain. Replace `localhost` with your configured domain (e.g. `10.10.10.207` or `compadre-stt.duckdns.org`).

### 503 warming_up

**Symptom:** All requests return 503 with `"Service is warming up"`.

**Cause:** Models are still downloading or loading. This is normal after a fresh start or restart.

**Resolution:**

1. Wait for model loading to complete (can take 1-3 minutes depending on download speed)
2. Monitor progress: `docker compose logs -f`
3. Check health endpoint: `curl -k https://localhost/health`
4. If 503 persists beyond 5 minutes, check logs for download or loading errors

### 401 authentication error

**Symptom:** Requests return 401.

**Possible causes and resolutions:**

1. **Missing or malformed `Authorization` header.** Verify the request uses `Authorization: Bearer cmpd_live_<plaintext>` (note the space after `Bearer`).
2. **Key not in the registry.** Confirm the plaintext you are sending matches a key listed in `compadre-keys list-keys`. The 8-character prefix shown there is the first 8 characters after `cmpd_live_`.

   ```bash
   docker compose exec compadre-stt compadre-keys list-keys
   ```

3. **Key is suspended or revoked.** `compadre-keys show <prefix>` reveals the status. Use `compadre-keys update-key` or `issue-key` to restore access as appropriate.
4. **Key is past `paid_until`.** A manual-billing key whose `paid_until` has lapsed authenticates as suspended. Either mark it paid through the billing workflow, extend it, or rotate.
5. **Cache state unexpectedly stale after a CLI mutation.** `compadre-keys` mutations touch a sentinel file (`{KEY_REGISTRY_DB_PATH}.invalidate`) that the API process stats on every cache read. If you see stale auth after a CLI mutation, confirm the container and the host see the same registry directory: the CLI must be invoked via `docker compose exec compadre-stt compadre-keys ...` (or the host path must be bind-mounted to the same volume used inside the container). Otherwise the sentinel the CLI writes is in a different filesystem than the one the API is watching and invalidation never fires. `docker compose restart compadre-stt` is the unconditional fallback.

### 400 upload size exceeded

**Symptom:** Requests return 400 with `"File size exceeds 25 MB limit"`.

**Cause:** The uploaded audio file is larger than 25 MB.

**Resolution:**

1. Compress the audio file (lower bitrate, mono instead of stereo)
2. Split long recordings into segments under 25 MB
3. The limit is enforced by both FastAPI middleware and Caddy's bounded-payload policy

### 400 audio duration exceeded

**Symptom:** Requests return 400 with `"Audio duration exceeds 600 seconds limit"`.

**Cause:** The uploaded audio is longer than 10 minutes.

**Resolution:**

1. Split the audio into segments under 10 minutes
2. The limit is enforced by `ffprobe` before the engine is invoked

### 400 unsupported audio format

**Symptom:** Requests return 400 with `"Unsupported audio format"`.

**Cause:** The uploaded file format is not recognized by `ffprobe`.

**Resolution:**

1. Use one of the supported formats: OPUS (in OGG), MP3, M4A, WAV, WebM
2. Re-encode the file if necessary: `ffmpeg -i input.flac -c:a libopus output.opus`

### 429 rate limit exceeded

**Symptom:** Requests return 429 with `"Rate limit exceeded"` and a `Retry-After: <N>` header alongside `X-RateLimit-Limit` / `X-RateLimit-Remaining` / `X-RateLimit-Reset`.

**Cause:** The API key used in the request has exceeded its `rate_limit_rpm` within the rolling 1-minute window. The limit is per-key, not per-IP, and the bucket is keyed on the registry's `key_id`.

**Resolution:**

1. Wait for the `Retry-After` window and retry. `Retry-After` is dynamic — it reports the actual seconds until the oldest hit in the window falls off, clamped to a minimum of 1.
2. To raise this customer's cap permanently, use the `compadre-keys` CLI (no container restart needed):

   ```bash
   docker compose exec compadre-stt compadre-keys update-key <key-prefix> --rpm 120
   ```

   `--rpm` accepts values 1..10 000. Every `compadre-keys` mutation touches the registry invalidation sentinel, so the change takes effect on the very next request — no cache-TTL wait.
3. Inspect recent rate-limit behaviour for a specific key:

   ```bash
   docker compose exec compadre-stt compadre-keys show <key-prefix>
   ```

4. For deployments that scale beyond `replicas: 1`, remember that rate-limit buckets are per-replica and the effective RPM is `replicas × rate_limit_rpm`. See [rate-limiting.md](rate-limiting.md).

### Request timeout

**Symptom:** Request hangs and eventually times out (120 second limit).

**Cause:** The audio is too long, or the service is processing another request (single FIFO queue), or the engine is warming up.

**Resolution:**

1. Check queue depth: `curl -k https://localhost/health | jq .queue_depth`
2. For 10-minute audio at RTF ~1.15, expect ~11.5s processing time per request; queue depth adds linearly
3. If queue is consistently backed up, consider scaling horizontally

### Empty transcription result

**Symptom:** Response returns 200 but the text is empty or contains only whitespace.

**Cause:** The audio contains only silence, background noise, or speech in a language different from the one specified (or auto-detected).

**Resolution:**

1. Check the audio actually contains speech at a reasonable volume level
2. Verify the `language` parameter matches the spoken language (default is `pt` for Portuguese)
3. For mixed-language audio, try omitting `language` to enable auto-detect
4. Check audio quality — heavy background noise or very low volume can cause empty results

### Transcription timeout (504/500)

**Symptom:** Request returns 504 from Caddy or 500 from the app after a long wait.

**Cause:** Queue is backed up, or the audio is at the maximum duration limit, or the engine is experiencing an error.

**Resolution:**

1. Check queue depth: `curl -k https://localhost/health | jq .queue_depth`
2. Check engine status: `curl -k https://localhost/health | jq .engines.whisper.loaded`
3. Check logs for errors: `docker compose logs --tail=50`
4. If the model failed to load, verify disk space and HuggingFace cache integrity
4. Try a different reference recording with clearer audio

## HTTPS / Caddy Issues

### Caddy certificate error

**Symptom:** Browser or curl shows SSL certificate error when connecting via HTTPS.

**Cause:** Using `DOMAIN=localhost` (self-signed cert) or DNS not pointing to the VPS IP.

**Resolution:**

1. For public domains: verify DNS A record points to the VPS IP
2. For private IPs: use `curl -k` or configure clients to accept self-signed certs
3. Check Caddy logs: `docker compose logs caddy`
4. Verify Caddy can reach the STT service: `docker compose exec caddy wget -qO- http://compadre-stt:8000/health`

### `compadre-keys` CLI errors

**Symptom:** `compadre-keys` commands exit with "Configuration error: KEY_REGISTRY_ENCRYPTION_KEY is not set" or database errors.

**Resolution:**

1. Confirm you are running the CLI inside the container (or against the same registry volume):

   ```bash
   docker compose exec compadre-stt compadre-keys list-customers
   ```

2. Check that `.env` on the host has `KEY_REGISTRY_ENCRYPTION_KEY` populated (`grep KEY_REGISTRY_ENCRYPTION_KEY ~/compadre/.env`). If the CLI is being run outside the container, the same env var must be exported in the calling shell.
3. For "Database error" messages, verify the registry path is writable and the volume is mounted: `docker compose exec compadre-stt ls -la /var/lib/compadre/registry.db`.
4. The CLI exit codes map to: 0 success, 2 usage error, 3 not found, 5 database error, 6 configuration error (see `compadre-keys --help`).

### `compadre-keys cache-status` reports "unavailable"

**Symptom:** `compadre-keys cache-status` prints the propagation bound message but `Cache health: unavailable (no running API instance).` The same breadcrumb appears in `show <key-prefix> --audit-tail N` output as `Last-24h stats: unavailable.`

**Cause:** The CLI globs per-replica counter snapshots at `{KEY_REGISTRY_DB_PATH}.counters.*.json` (each replica writes `{db}.counters.{hostname}.json` every 15 s). "Unavailable" means no replica's file matched — the glob came back empty, or every candidate failed to parse. A stale snapshot is NOT unavailable; it is surfaced with an age label like `snapshot written 2m ago`.

**Resolution:**

1. Confirm the API process is actually running: `docker compose ps compadre-stt` (status should be `running`, not `restarting`). A 200 response from `GET /health` is also a good liveness probe.
2. Check that at least one replica has written a snapshot:

   ```bash
   docker compose exec compadre-stt ls -la /var/lib/compadre/registry.db.counters.*.json
   ```

   If the glob comes up empty, the service may still be in the startup warm-up phase; wait ~20 s and retry. If it stays empty, check the service logs for `Counter snapshot write` warnings.
3. If the CLI runs on the host (not inside the container) and the container mounts a named Docker volume, the host path `./data/keys/` is unrelated to the in-container path `/var/lib/compadre/`. Set `KEY_REGISTRY_DB_PATH` in your host shell to the mounted location, or run the CLI inside the container (`docker compose exec compadre-stt ...`).
4. The "unavailable" state is never an error — `compadre-keys cache-status` exits 0 by design. If you need a non-zero signal in scripts, check `.snapshot_available` from `--format json`.

### UFW blocking access

**Symptom:** Cannot reach the VPS on ports 80/443 but SSH works.

**Cause:** UFW rules may have been modified or ports were not allowed.

**Resolution:**

1. Check UFW status: `sudo ufw status`
2. Ensure ports are open: `sudo ufw allow 80/tcp && sudo ufw allow 443/tcp`
3. Verify Docker is not conflicting: `sudo iptables -L -n | grep 443`

## Deployment Issues

### Health check fails after update

**Symptom:** The update playbook fails at the health check step after rebuilding.

**Cause:** Code change introduced a startup error, or model files are incompatible with new code.

**Resolution:**

1. Check logs: `docker compose -f ~/compadre/compose.yaml logs --tail=50`
2. Check container status: `docker compose -f ~/compadre/compose.yaml ps`
3. Try restarting: `docker compose -f ~/compadre/compose.yaml restart`
4. Roll back to previous version:

    ```bash
    cd ~/compadre
    git log --oneline -5
    git checkout <previous-commit>
    docker compose up -d --build
    ```

### Volume name mismatch

**Symptom:** Models or voices are not found after a rebuild, even though they were previously deployed.

**Cause:** Docker Compose prefixes volume names with the project directory name unless volumes have explicit `name:` keys.

**Resolution:**

1. Verify compose.yaml has explicit `name:` for each volume:

    ```yaml
    volumes:
      compadre-models:
        name: compadre-models
    ```

2. Check existing volumes: `docker volume ls | grep compadre`
3. If volumes have wrong names, copy data to correctly named volumes and remove old ones

### Disk space

**Symptom:** Service becomes slow or fails to transcribe. Disk usage warnings in health playbook.

**Cause:** HuggingFace model cache or Docker logs consuming disk.

**Resolution:**

1. Check disk usage: `df -h /`
2. Check model volume size: `docker compose exec compadre-stt du -sh /app/models`
3. Check Docker log size: `docker system df -v | grep compadre`
4. Prune old Docker logs if needed: `docker compose exec compadre-stt sh -c 'truncate -s 0 /var/log/* 2>/dev/null || true'`
5. The bootstrap playbook installs log rotation (`max-size: 10m, max-file: 5` in compose.yaml)

## Log Format

Compadre uses structured pipe-delimited logging:

```text
2026-03-25T14:30:00Z | INFO  | request | STT transcription completed in 11.5s
2026-03-25T14:30:01Z | ERROR | request | Unhandled exception during request processing
```

**Format:** `timestamp | level | component | message`

**Components:**

| Component | Description |
| --- | --- |
| `startup` | Application initialization and model loading |
| `download` | Model download progress |
| `engine` | Whisper inference engine events |
| `request` | HTTP request handling, STT transcription, and errors |
| `access` | HTTP access log (client IP, method, path, status, duration) |
| `health` | Health endpoint checks |

**Filtering logs by component:**

```bash
# Show only request logs
docker compose logs | grep '| request |'

# Show only errors
docker compose logs | grep '| ERROR |'

# Follow logs in real time
docker compose logs -f --tail=20
```
