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

### espeak-ng pt_extra not found

**Symptom:** Kokoro engine loads but sigla pronunciation is incorrect (spells letters instead of pronouncing as words).

**Cause:** The custom pt_extra dictionary was not compiled into espeak-ng data.

**Resolution:**

1. Verify the Dockerfile copies pt_extra data during build
2. Rebuild the image: `docker compose up -d --build`
3. Check that `ESPEAK_DATA_PATH` points to the correct directory

### qwen3-tts-cli binary missing

**Symptom:** Qwen3 engine fails to load. Health endpoint shows `qwen3.loaded: false`.

**Cause:** The `qwen3-tts-cli` binary is not present at `QWEN3_CLI_PATH`.

**Resolution:**

1. Verify the binary exists inside the container: `docker compose exec compadre-stt ls -la /usr/local/bin/qwen3-tts-cli`
2. Check the Dockerfile copies the binary from `deploy/bin/qwen3-tts-cli`
3. If rebuilding the binary is needed, see [building-qwen3-tts-cli.md](building-qwen3-tts-cli.md)

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

### 404 voice not found

**Symptom:** Requests return 404 with `"Voice 'name' not found"`.

**Cause:** The requested voice name is not in the available voices list.

**Resolution:**

1. Check available voices via health endpoint: `curl -k https://localhost/health | jq .voices`
2. Voice names use the `engine/name` format: `kokoro/pm_santa`, `kokoro/pf_dora`, `kokoro/pm_alex`, `qwen/female`, `qwen/galvani`, etc.
3. Voice names are case-sensitive. The engine prefix (`kokoro/` or `qwen/`) is required. See [voice-management.md](voice-management.md) for the full list of available voices and how to add new ones.

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

**Cause:** Text is too long for the selected engine, or the service is processing another request (single FIFO queue).

**Resolution:**

1. Reduce text length (max 500 chars for Kokoro, 300 chars for Qwen3)
2. Check queue depth: `curl -k https://localhost/health | jq .queue_depth`
3. If queue is consistently backed up, consider scaling horizontally

## Engine-Specific Issues

### Kokoro sigla mispronunciation

**Symptom:** Siglas like CPF, CNPJ, or FGTS are spelled letter-by-letter instead of pronounced correctly.

**Cause:** The espeak-ng pt_extra dictionary is not loaded. The bundled `espeakng_loader` data takes precedence over `ESPEAK_DATA_PATH`.

**Resolution:**

1. Verify pt_extra was compiled: check Dockerfile for the espeak-ng dictionary build step
2. The compiled pt_extra data must be copied to BOTH the system path AND the espeakng_loader bundled path inside the container
3. Rebuild the image: `docker compose up -d --build`

### Qwen3 random accent

**Symptom:** Qwen3 output has inconsistent or random accent (sometimes sounds like Mandarin or English instead of Brazilian Portuguese).

**Cause:** The `-l pt` flag is missing from the qwen3-tts-cli invocation. Without it, the language mode is undefined.

**Resolution:**

1. Check service.py to verify the `-l pt` flag is passed to the CLI command
2. This should be handled automatically by the application code
3. If the issue persists, check the qwen3-tts-cli binary version: `docker compose exec compadre-stt qwen3-tts-cli --version`

### Clone voice sounds different

**Symptom:** The cloned voice does not match the reference audio closely, or sounds different between runs.

**Cause:** Reference WAV quality issues, or seed value not producing consistent results.

**Resolution:**

1. Verify reference WAV quality: 3-10 seconds, mono, 16kHz+, clear speech, minimal background noise
2. Check the reference file exists: `docker compose exec compadre-stt ls -la /voices/`
3. Seed reproducibility gives similar (not identical) results across runs -- some variation is expected
4. Try a different reference recording with clearer audio

## HTTPS / Caddy Issues

### Caddy certificate error

**Symptom:** Browser or curl shows SSL certificate error when connecting via HTTPS.

**Cause:** Using `DOMAIN=localhost` (self-signed cert) or DNS not pointing to the VPS IP.

**Resolution:**

1. For public domains: verify DNS A record points to the VPS IP
2. For private IPs: use `curl -k` or configure clients to accept self-signed certs
3. Check Caddy logs: `docker compose logs caddy`
4. Verify Caddy can reach the TTS service: `docker compose exec caddy wget -qO- http://compadre-stt:8000/health`

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

2. Check existing volumes: `docker volume ls | grep comadre`
3. If volumes have wrong names, copy data to correctly named volumes and remove old ones

### Disk space

**Symptom:** Service becomes slow or fails to generate audio. Disk usage warnings in health playbook.

**Cause:** Generated audio files accumulate over time.

**Resolution:**

1. Check disk usage: `df -h /`
2. Check audio volume size: `docker compose exec compadre-stt du -sh /audio`
3. Verify the audio cleanup cron is active: `crontab -l | grep compadre-audio-cleanup`
4. Expected: runs every 6 hours, deletes audio files older than 24 hours
5. Manual cleanup: `docker compose exec compadre-stt find /audio -type f \( -name '*.ogg' -o -name '*.wav' \) -mmin +1440 -delete`

## Log Format

Comadre uses structured pipe-delimited logging:

```text
2026-03-25T14:30:00Z | INFO  | request | TTS generation completed in 8.3s
2026-03-25T14:30:01Z | ERROR | request | Unhandled exception during request processing
```

**Format:** `timestamp | level | component | message`

**Components:**

| Component | Description |
| --- | --- |
| `startup` | Application initialization and model loading |
| `download` | Model download progress |
| `request` | HTTP request handling, TTS generation, and errors |
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
