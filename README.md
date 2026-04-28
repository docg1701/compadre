# Compadre

<p align="center">
  <img src="assets/logo-compadre.webp" alt="Compadre Logo">
</p>

<p align="center">
  <img src="https://img.shields.io/badge/python-3.12+-blue?logo=python&logoColor=white" alt="Python 3.12+">
  <img src="https://img.shields.io/badge/version-1.2.0-blue" alt="Version 1.2.0">
  <img src="https://img.shields.io/badge/FastAPI-0.115+-009688?logo=fastapi&logoColor=white" alt="FastAPI">
  <img src="https://img.shields.io/badge/STT-faster--whisper__large--v3--turbo-8b5cf6" alt="faster-whisper STT">
  <img src="https://img.shields.io/badge/license-MIT-green" alt="MIT License">
</p>

**Compadre Ouve Mensagem Por Áudio, Devolve Resposta Escrita**

Self-hosted STT API optimized for Brazilian Portuguese. faster-whisper large-v3-turbo with int8 quantization, CPU-only, network-isolated. OpenAI Whisper API-compatible endpoint with word-level timestamps and multiple output formats (text, JSON, SRT, VTT).

## Why Compadre — Cost & Performance at a Glance

Compadre exists for one reason: **scaling WhatsApp audio transcription without paying per-minute SaaS bills**. OpenAI Whisper API and ElevenLabs Scribe get expensive fast — Compadre's cost is the VPS, full stop, regardless of how much audio you transcribe.

| What you get | Single replica (~$7/mo VPS) | Two replicas (~$21/mo VPS) |
|---|---|---|
| Sustainable throughput | **~5 audio-minutes transcribed per wall-clock minute** | **~9 audio-minutes transcribed per wall-clock minute** |
| WhatsApp messages (avg 30s) | ~600 messages/hour | ~1,080 messages/hour |
| Realistic monthly capacity (8h × 22 working days) | ~52,800 messages | **~95,000 messages** |
| Realtime factor (1 min audio → seconds to transcribe) | ≤ 18 s (RTF ≤ 0.3) | ≤ 18 s per replica |
| Concurrent requests handled (FIFO queued) | 1 in-flight + queue | 2 in-flight + queue (least_conn LB) |
| LGPD / data privacy | Inherent — audio never leaves your server | Same |
| Per-minute cost at saturation | **$0.00022/min** | **$0.00065/min** |

**Cost comparison at WhatsApp bot scale (~100,000 audios/month, avg 30 s = 50,000 minutes = 833 hours of audio):**

| Platform | Monthly cost | Pricing model |
|---|---|---|
| **Compadre 2 replicas** | **~$21** | Fixed VPS — does not grow with volume |
| OpenAI Whisper API | ~$300 | $0.006 per audio minute, scales linearly |
| ElevenLabs Scribe | ~$250 | $0.30 per audio hour, scales linearly |

Compadre is **~14x cheaper than OpenAI Whisper API** and **~12x cheaper than ElevenLabs Scribe** at this volume. Above this tier the gap only widens — Compadre's marginal cost is the next VPS upgrade (~$15–25 to double capacity), while the SaaS competitors keep charging per audio minute forever.

**When does Compadre pay off?** The crossover is around 20–25 hours of audio per month. Below that, the SaaS APIs are cheaper than a dedicated VPS. Above that, every additional minute costs you nothing extra with Compadre. If your n8n WhatsApp bot processes more than ~1,000 audio messages per month, you're already saving money.

**Why this works:** Each Compadre replica is CPU-bound and transcribes a roughly constant amount of audio per second of wall clock. Two replicas behind a queue-aware Caddy load balancer (`lb_policy least_conn`) extract the maximum throughput a shared 8-CPU host can deliver. The single-FIFO-lock-per-replica design means Caddy's active-connection count equals queued + in-flight requests, giving the load balancer accurate queue-depth visibility.

## Quick Start

```bash
# Clone and configure
git clone git@github.com:docg1701/compadre.git
cd compadre
cp .env.example .env
# Generate the registry encryption key and paste it into .env as
# KEY_REGISTRY_ENCRYPTION_KEY=<value>:
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"

# Build and run
docker compose up --build -d

# Issue the first customer + API key via the compadre-keys CLI
docker compose exec compadre-stt compadre-keys create-customer \
  --name "Acme Demo" --email ops@acme.example --tax-id 11144477735
docker compose exec compadre-stt compadre-keys issue-key \
  --customer <cust_id_from_previous_step> --rpm 60 --price 149
# The issued plaintext (cmpd_live_...) is shown once — copy it now.

# Test transcription (text output)
curl -k -X POST https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -F "file=@audio.opus" \
  -F "response_format=text"

# Test with verbose JSON (word-level timestamps)
curl -k -X POST https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -F "file=@audio.opus" \
  -F "response_format=verbose_json"

# Test SRT subtitle output
curl -k -X POST https://localhost/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -F "file=@audio.opus" \
  -F "response_format=srt"
```

## How It Works

```text
POST /v1/audio/transcriptions  multipart/form-data with audio file
    |
Auth & rate-limit (compadre-keys registry, per-key RPM bucket)
    |
Acquire FIFO queue lock (one transcription at a time per replica)
    |
Decode audio (ffmpeg → 16 kHz mono PCM s16le WAV)
    |
Transcribe (faster-whisper large-v3-turbo, int8, beam_size=5)
    |
Format output (text / json / verbose_json / srt / vtt)
    |
Return text response with word-level timestamps
```

## Engine

| Parameter | Value | Notes |
|---|---|---|
| Model | `large-v3-turbo` | Best speed/accuracy tradeoff for CPU |
| Backend | CTranslate2 (int8) | Quantized inference, optimized for CPU |
| Beam size | 5 | Battle-tested for pt-BR conversational audio |
| Word timestamps | Enabled | Required for `verbose_json`, `srt`, `vtt` output |
| Language | `pt` (default) | Override via `language` param; omit for auto-detect |
| Condition on previous text | Disabled | Prevents hallucination cascades on long audio |
| Threads | 4 (configurable) | Matches 4 vCPU reference hardware per replica |

**Audio preprocessing**: Compadre uses `ffmpeg` to decode any input format (OPUS in OGG, MP3, M4A, WAV, WebM) into 16 kHz mono PCM s16le WAV before passing to the Whisper engine. Upload limits: 25 MB max file size, 10 minutes max duration (validated via `ffprobe` before transcription).

## Response Formats

| Format | Content-Type | Description |
|---|---|---|
| `text` (default) | `text/plain` | Plain transcription string |
| `json` | `application/json` | `{"text": "..."}` |
| `verbose_json` | `application/json` | Full response: `text`, `language`, `duration`, `segments` with word-level `start`/`end` timestamps |
| `srt` | `text/plain` | SubRip subtitle format with per-segment timestamps |
| `vtt` | `text/plain` | WebVTT subtitle format with per-segment timestamps |

## Key Features

- **OpenAI-compatible API** (`POST /v1/audio/transcriptions`) — drop-in replacement for `openai.audio.transcriptions.create()`. Accepts `multipart/form-data` with the same parameter names (`file`, `model`, `language`, `response_format`, `temperature`).
- **Word-level timestamps** — every transcription includes precise `start`/`end` offsets per word, enabling subtitle generation and timestamped transcripts.
- **SRT and VTT subtitle output** — generate subtitles directly from the API, no post-processing needed.
- **Auto language detection** — omit the `language` parameter to let Whisper detect the spoken language; set `language=pt` for guaranteed pt-BR output.
- **CPU-only, network-isolated** — runs on a 4 vCPU / 8 GB RAM VPS or any PC. Zero external API calls after model download.
- **Single FIFO queue** per instance (`asyncio.Lock`) — scale horizontally via `deploy.replicas` in compose.yaml. Caddy `least_conn` load balancer distributes across replicas with queue-depth awareness.
- **HTTPS via Caddy** — auto-TLS reverse proxy with Let's Encrypt or self-signed certs.
- **Browser landing page** — public landing page at the root path for uploading audio and getting transcriptions from any device (bring your own API key).
- **Registry-backed API keys** — issue, rotate, and revoke keys via the `compadre-keys` CLI. Keys are stored as SHA-256 hashes in an encrypted SQLite registry; tax IDs are Fernet-encrypted at rest.
- **Billing lifecycle verbs** — `compadre-keys mark-paid <prefix> --until YYYY-MM-DD` confirms a payment to an exact date (auto-reactivates suspended keys); `compadre-keys mark-paid <prefix> --months <1..36>` extends from `paid_until` (if today-or-later) or from today (if expired); both forms accept `--price-monthly <brl>` to update price atomically in the same transaction. `compadre-keys suspend <prefix>` / `compadre-keys reactivate <prefix>` are the manual out-of-cycle levers.
- **Billing reports** — `compadre-keys at-risk --window 5d` previews active manual keys expiring in the next N days (the Day-04 chase list); `compadre-keys revenue --month current` summarises paid / pending / suspended / complimentary counts, MRR and projected revenue (the Day-05 screenshot). Add `--as-of YYYY-MM-DD` to replay the audit log and reconstruct the same report as it would have looked on a past day (`BILLING_TIMEZONE`-aware day boundaries).
- **Automated suspension sweep** — a daily cron at 00:30 in the configured `BILLING_TIMEZONE` (default **UTC**) auto-suspends manual keys whose `paid_until` has elapsed, writing one audit row per key plus a summary row for liveness tracking. Run `compadre-keys suspend-sweep --dry-run` to preview the set without writing, or `compadre-keys suspend-sweep` to manually recover from a failed cron.
- **Per-key rate limiting** — each API key carries its own `rate_limit_rpm`. Customers on higher tiers get higher throughput; customers on smaller tiers are bounded predictably. Buckets are keyed on the registry's `key_id` and stable across key rotation. See [docs/rate-limiting.md](docs/rate-limiting.md).
- **Operator observability** — per-key counters (`requests_total`, `rate_limit_429`, `peak_rpm`) snapshot every 15 s to disk. Inspect the auth cache with `compadre-keys cache-status`; inspect a specific key's last-24h activity with `compadre-keys show <key-prefix> --audit-tail N`.
- **System health** — `compadre-keys health` renders a single panel that covers DB integrity and `0600` permissions, schema-version parity, customer / key counts, last suspension-sweep age, cron-installed status, backup liveness (warn unless a fresh `*.backup-*.tar.gz.enc` sits next to the DB), and auth-cache replica telemetry. Pipe `compadre-keys health --format json` into a Nagios / Prometheus textfile monitor — exit 0 on healthy, exit 7 on any warning or failure.
- **LGPD data operations** — `compadre-keys export-customer <cust_id> --format json|yaml` emits the customer's stored data (decrypted tax ID, name, email, notes, associated key prefixes) as a machine-readable document for Art. 18 II/V access responses; add `--include-audit` to attach the chronological audit-event array (with `audit_events_total` / `audit_events_truncated` completeness signals) when counsel reads Art. 18 broadly. `compadre-keys update-customer <cust_id> --name "..." | --email "..." | --add-note "..." | --notes "..."` covers Art. 18 III corrections; `compadre-keys correct-tax-id <cust_id> --new-cpf "..."|--new-cnpj "..." --reason "..."` corrects a CPF/CNPJ while preserving `customer_id` and every API-key FK. `compadre-keys delete-customer <cust_id> --hard` irreversibly purges the customer's personal data and all their API keys in a single transaction, preserving the tombstone row and audit trail per Art. 18 VI.
- **Encryption-key rotation** — `compadre-keys rotate-encryption-key --new-key <fernet>` atomically re-encrypts every active customer's tax ID under a freshly generated Fernet key in a single transaction, audits the event, and prints the value to drop into `.env` — the breach-response lever for a suspected key exposure without taking the registry offline.
- **Nightly encrypted backups** — `ansible-playbook deploy/ansible/playbooks/backup-keys.yml` produces `{registry.db}.backup-<UTC-ISO>.tar.gz.enc` at mode 0600, AES-256-CBC + PBKDF2 envelope. The companion `restore-keys.yml -e "backup_file=..."` playbook performs the idempotent rollback. 24-hour RPO, < 10-minute RTO for ≤ 10 000 customers.
- **Operational health assertion playbook** — `ansible-playbook deploy/ansible/playbooks/health.yml` runs a chain of `ansible.builtin.assert` probes covering DB file permissions (`0600`), parent dir mode (`0700`), both required cron entries, backup-archive presence, UFW rules unchanged, and the in-container `compadre-keys health` aggregate. Each assertion carries a corrective hint in its `fail_msg`; the playbook exits non-zero on any deviation.

## Requirements

- 4 vCPUs, 8 GB RAM (VPS or bare metal)
- ~3 GB disk for model (large-v3-turbo auto-downloads from HuggingFace on first startup)
- Docker and Docker Compose v2 (Caddy reverse proxy handles all external access on ports 80/443)
- `ffmpeg` available in the container (included in the Compadre Docker image)

## Deployment

Full lifecycle management via Ansible:

```bash
cd deploy/ansible
ansible-playbook playbooks/bootstrap.yml -i inventory/production.yml -e "ansible_become_password=YOUR_SUDO_PASS"  # Deploy
ansible-playbook playbooks/update.yml -i inventory/production.yml     # Update
ansible-playbook playbooks/health.yml -i inventory/production.yml     # Diagnose
ansible-playbook playbooks/cleanup.yml -i inventory/production.yml -e "ansible_become_password=YOUR_SUDO_PASS"   # Remove
```

## Documentation

- [docs/api-reference.md](docs/api-reference.md) — API endpoint spec, schemas, error codes, curl examples
- [docs/deployment-guide.md](docs/deployment-guide.md) — Deployment lifecycle: bootstrap, update, cleanup, HTTPS, key registry bootstrap
- [docs/configuration-reference.md](docs/configuration-reference.md) — Environment variables reference
- [docs/monetization.md](docs/monetization.md) — Customer tiers, per-key pricing, rate-limit lifecycle
- [docs/monetization.md#lgpd-operator-obligations](docs/monetization.md#lgpd-operator-obligations) — LGPD operator handbook: controller identification, data-subject request procedures, breach-notification runbook, privacy-policy template paragraph
- [docs/rate-limiting.md](docs/rate-limiting.md) — Per-key algorithm, bucket identity, 429 semantics, operator controls
- [docs/troubleshooting.md](docs/troubleshooting.md) — Common issues with resolution steps
- [docs/api-architecture.md](docs/api-architecture.md) — Internal API design, queue architecture, transcription pipeline

## Load Testing

```bash
# Saturation test (concurrency-based)
uv run --group test python scripts/loadtest.py \
  --url https://YOUR_VPS_DOMAIN --api-key cmpd_live_YOUR_PLAINTEXT --scenario all

# Capacity test (rate-based, WhatsApp context)
uv run --group test python scripts/capacity_test.py \
  --url https://YOUR_VPS_DOMAIN --api-key cmpd_live_YOUR_PLAINTEXT
```

Issue a dedicated load-test key via `compadre-keys issue-key --rpm 10000 --billing-mode complimentary`, and `compadre-keys revoke-key` it when the run is done.

## Development

```bash
uv sync                                    # Install dependencies
uv run pytest                              # Run tests
uv run ruff check . && uv run mypy src/    # Lint + type check
uv run pre-commit run --all-files          # All quality checks
```

## Roadmap

- **v1.1 — Multi-speaker diarization**: transcribe audio with two or more speakers, return structured dialogue with speaker labels and optional speaker enrollment (pre-register known voices so output uses real names instead of `S1`/`S2`).

## License

[MIT](LICENSE)
