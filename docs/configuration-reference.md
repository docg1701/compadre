# Configuration Reference

Compadre is configured via environment variables. All variables are loaded at startup using pydantic-settings. The single required variable is `KEY_REGISTRY_ENCRYPTION_KEY`; everything else has sensible defaults.

**Last updated:** 2026-04-26

## Environment Variables

| Variable | Default | Description | Example |
| --- | --- | --- | --- |
| `KEY_REGISTRY_ENCRYPTION_KEY` | *(required)* | Fernet key used to encrypt tax IDs at rest in the SQLite key registry. Must be a valid 44-character base64 Fernet key. Startup fails if missing or malformed. Uses `SecretStr` internally so the value never appears in logs or error renderings. | *(output of `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`)* |
| `KEY_REGISTRY_DB_PATH` | `./data/keys/registry.db` | SQLite registry file path. Parent directory is created with mode 0700 and the file with mode 0600 on first use. WAL sidecars inherit the same mode. | `/var/lib/compadre/registry.db` |
| `BACKUP_PASSPHRASE` | *(empty)* | Passphrase for AES-256 encryption of the nightly backup tarball. Must be ≥ 16 characters when set; empty is accepted at service boot so a deployment that hasn't yet re-run `bootstrap.yml` with the new env var can still start the auth path. Consumed only by `deploy/ansible/playbooks/backup-keys.yml` and `restore-keys.yml` — the FastAPI app and every `compadre-keys` CLI verb ignore it. Uses `SecretStr` so the value never appears in logs or error renderings. | *(output of `python -c "import secrets; print(secrets.token_urlsafe(32))"`)* |
| `OPERATOR_CONTACT_HINT` | `Contact your Compadre operator for billing.` | Operator contact string echoed in 401 hints for suspended / revoked / past-due keys. Keep it short and customer-facing. | `Contact ops@acme.example for billing.` |
| `BILLING_TIMEZONE` | `UTC` | IANA timezone used for billing-day calculations (first-charge dates, `paid_until` comparisons, stamped note dates) and the daily suspension sweep's day-boundary. Must be a valid zone; startup fails otherwise. **MUST match the `CRON_TZ` envvar set by `deploy/ansible/playbooks/bootstrap.yml`** (sourced from the inventory's `billing_timezone` variable) — drift between the two silently shifts the daily suspension sweep's "today" computation relative to the cron schedule. **Billing-TZ is a contractual input**: whichever zone this variable holds dictates when a customer's `paid_until` expires and their API key flips to 401. Operators MUST communicate the chosen zone to customers at contract time and avoid changing it post-onboarding (a change silently shifts when existing keys go past-due). Default `UTC` is the operator-neutral, internationally predictable choice; override only when you have formally informed the customer base. See also: `compadre-keys mark-paid`, `compadre-keys suspend`, `compadre-keys reactivate` for the operator-hand billing verbs; `compadre-keys at-risk`, `compadre-keys revenue` read the reporting month / look-ahead window in this timezone; `compadre-keys suspend-sweep` is the manual/dry-run entry point for the daily auto-suspension cron. | `America/Sao_Paulo` |
| `BILLING_DAY` | `5` | Day-of-month (1–28) for monthly billing sweeps. Kept inside 1..28 so every month has the date. | `10` |
| `WHISPER_MODEL_SIZE` | `large-v3-turbo` | faster-whisper model size. Only `large-v3-turbo` is supported in MVP. | `large-v3-turbo` |
| `WHISPER_COMPUTE_TYPE` | `int8` | CTranslate2 compute type for CPU inference. `int8` is the default for CPU-only deployments. | `int8` |
| `WHISPER_CPU_THREADS` | `4` | Number of CPU threads for Whisper inference (≥1). Matches 4 vCPU reference hardware. | `4` |
| `WHISPER_LANGUAGE` | `pt` | Default transcription language. ISO 639-1 code (`pt`, `en`, `es`, etc.). Set to empty string for auto-detect. Omitting the env var uses `pt`. | `pt` |
| `HF_HOME` | `./data/cache/huggingface` | HuggingFace Hub cache directory for model auto-download. For air-gapped deployments, pre-seed this directory. | `/app/models` |
| `MAX_UPLOAD_SIZE_MB` | `25` | Maximum audio upload size in megabytes (1–100). Enforced by FastAPI middleware and Caddy bounded-payload policy. | `25` |
| `MAX_AUDIO_DURATION_SECONDS` | `600` | Maximum audio duration in seconds (≥1). Enforced by `ffprobe` before engine invocation. | `600` |
| `PORT` | `8000` | HTTP port for the FastAPI server (1..65535). Exposed only inside the Docker network — Caddy fronts 80/443. | `9000` |
| `DOMAIN` | `localhost` | Domain name used by Caddy. A real domain triggers Let's Encrypt; `localhost` uses a self-signed cert from Caddy's internal CA. Compose-level variable, not consumed by the Python process. | `stt.example.com` |

## Auxiliary files

The key registry lives at `KEY_REGISTRY_DB_PATH`, and three sibling files share the same parent directory and mode `0600`:

| Path | Purpose |
| --- | --- |
| `{KEY_REGISTRY_DB_PATH}.invalidate` | Cache-invalidation sentinel. The `compadre-keys` CLI touches this file on every `api_key` mutation; the API stats it on each cache read and flushes the `AuthCache` the moment mtime advances. See [rate-limiting.md](rate-limiting.md#operator-surface). |
| `{KEY_REGISTRY_DB_PATH}.counters.{replica_id}.json` | Per-replica request counters snapshot written every 15 s by the API process (one file per replica, `{replica_id}` = `socket.gethostname()`). The `compadre-keys cache-status` and `show <key-prefix> --audit-tail N` commands glob every matching file and aggregate. See [rate-limiting.md](rate-limiting.md#counters). |
| `{KEY_REGISTRY_DB_PATH}.cron-installed` | Cron-installed marker for the daily suspension sweep, written by `deploy/ansible/playbooks/bootstrap.yml` and removed by `cleanup.yml`. Two ASCII `key=value` lines (`schedule=...`, `cron_tz=...`), mode `0600`. `compadre-keys health` reads this to report the cron's installed status; the container cannot read the host crontab, so the marker is the only source of truth for the CLI. **Do not hand-edit** — manual changes drift from the real crontab and mislead `health`'s cron check. |
| `{KEY_REGISTRY_DB_PATH}.backup-cron-installed` | Cron-installed marker for the nightly `compadre-keys-backup` cron, written by `bootstrap.yml` and removed by `cleanup.yml`. Three ASCII `key=value` lines (`schedule=...`, `cron_tz=...`, `retention_days=...`), mode `0600`. Consumed by both `health.yml` (ansible-layer assertion) **and** `compadre-keys health`'s dedicated `Backup cron` row (Story 4.4). The CLI row renders as `compadre-keys-backup at <schedule> (CRON_TZ=<tz>, retention=<days>d)` when the marker is well-formed (severity `ok`); a missing or unparseable marker flips the row to `warn` and the JSON-mode aggregate to `is_healthy=false` (exit 7). |
| `{KEY_REGISTRY_DB_PATH}.backup-<UTC-ISO>.tar.gz.enc` | Nightly encrypted backup archive written by `deploy/ansible/playbooks/backup-keys.yml`. Contains `registry.db` (SQLite `.backup()` snapshot) + `encryption-key.txt` (the live Fernet key captured at backup time), tarred and encrypted with AES-256-CBC + PBKDF2 via the `BACKUP_PASSPHRASE` env var. Mode `0600`. Retention defaults to 14 days, tunable via the `backup_retention_days` ansible inventory var. `compadre-keys health` globs `*.backup-*.tar.gz.enc` in this directory to report the `Last backup` freshness row. |

None of these files is a configuration surface — the paths are derived from `KEY_REGISTRY_DB_PATH` and cannot be overridden. The CLI silently degrades when any of them is absent (e.g. API not running yet, or the cron has not been installed).

## Rate limiting is not env-configured

Per-key rate limits are set at key-issuance time via `compadre-keys issue-key --rpm <n>` and changed via `compadre-keys update-key --rpm <n>` (both capped at 1..10 000). There is no environment variable that sets a global RPM floor, ceiling, or per-IP limit — the registry row is authoritative. See [rate-limiting.md](rate-limiting.md) and [monetization.md](monetization.md).

## Docker Compose Environment

In production, environment variables are set in `compose.yaml` (not `.env` directly). The compose file maps host `.env` values to the `compadre-stt` container:

```yaml
compadre-stt:
  build: .
  expose:
    - "8000"
  volumes:
    - compadre-models:/app/models:rw
    - compadre-registry:/var/lib/compadre:rw
  environment:
    KEY_REGISTRY_ENCRYPTION_KEY: ${KEY_REGISTRY_ENCRYPTION_KEY:?KEY_REGISTRY_ENCRYPTION_KEY must be set}
    KEY_REGISTRY_DB_PATH: "${KEY_REGISTRY_DB_PATH:-/var/lib/compadre/registry.db}"
    OPERATOR_CONTACT_HINT: "${OPERATOR_CONTACT_HINT:-Contact your Compadre operator for billing.}"
    BILLING_TIMEZONE: "${BILLING_TIMEZONE:-UTC}"
    BILLING_DAY: "${BILLING_DAY:-5}"
    WHISPER_MODEL_SIZE: "${WHISPER_MODEL_SIZE:-large-v3-turbo}"
    WHISPER_COMPUTE_TYPE: "${WHISPER_COMPUTE_TYPE:-int8}"
    WHISPER_CPU_THREADS: "${WHISPER_CPU_THREADS:-4}"
    WHISPER_LANGUAGE: "${WHISPER_LANGUAGE:-pt}"
    HF_HOME: "${HF_HOME:-/app/models}"
    MAX_UPLOAD_SIZE_MB: "${MAX_UPLOAD_SIZE_MB:-25}"
    MAX_AUDIO_DURATION_SECONDS: "${MAX_AUDIO_DURATION_SECONDS:-600}"
    PORT: "8000"
  deploy:
    replicas: 1
    resources:
      limits:
        cpus: "4"
        memory: 6G
```

`KEY_REGISTRY_ENCRYPTION_KEY` uses the `?` syntax, so Docker Compose refuses to start if it is missing from `.env`. The STT service is not exposed on the host network — all external traffic flows through Caddy on 80/443. Scale horizontally by increasing `replicas` (see [rate-limiting.md](rate-limiting.md) for the per-replica rate-limit caveat).

## Complete .env.example

The canonical template lives at `.env.example` in the repository root. At the time of writing its contents are:

```bash
# Compadre — Environment Configuration
# Copy to .env and fill in values.

# Fernet key for encrypting tax IDs at rest in the key registry (REQUIRED).
# Generate with:
#   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
KEY_REGISTRY_ENCRYPTION_KEY=

# SQLite key registry path. Must match the in-container path used by both the
# compadre-stt container and the compadre-keys CLI (when run against the same
# mount). Compose bind-mounts the compadre-registry named volume at
# /var/lib/compadre, so the default below keeps the CLI and the service pointed
# at the same file.
KEY_REGISTRY_DB_PATH=/var/lib/compadre/registry.db

# Operator contact string shown in CLI error hints (e.g. your ops email).
OPERATOR_CONTACT_HINT=

# Domain for HTTPS (Caddy auto-provisions Let's Encrypt cert).
# Use "localhost" for a self-signed cert on private networks.
DOMAIN=localhost

# Default transcription language (ISO 639-1). Leave empty for auto-detect.
WHISPER_LANGUAGE=pt

# HuggingFace Hub cache directory for model auto-download.
HF_HOME=/app/models

# Max upload size (MB) and max audio duration (seconds).
MAX_UPLOAD_SIZE_MB=25
MAX_AUDIO_DURATION_SECONDS=600
```

Advanced overrides (`WHISPER_MODEL_SIZE`, `WHISPER_COMPUTE_TYPE`, `WHISPER_CPU_THREADS`, `BILLING_DAY`, `BILLING_TIMEZONE`, `PORT`) accept the defaults in the table above and can be set in `.env` only when a deployment needs to deviate.
