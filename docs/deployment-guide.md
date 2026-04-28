# Deployment Guide

Full lifecycle management for Compadre STT service using Ansible playbooks.

**Last updated:** 2026-04-27

## Fresh Deploy Runbook

Follow these steps to deploy Compadre on a clean VPS. Assume zero pre-installed software beyond the base OS and SSH.

### 1. Prerequisites Checklist

**Control machine** (your laptop/workstation):

- [ ] Ansible 2.14+ installed (`ansible --version`)

- [ ] `ansible-galaxy collection install -r deploy/ansible/requirements.yml` run

- [ ] Git repo cloned at `~/dev/compadre` (or equivalent)

- [ ] SSH key that can reach the VPS as the ansible user

- [ ] SSH agent running with GitHub key loaded (`ssh-add -l` shows your key) — needed for the git clone step during bootstrap. Alternatively, copy an SSH key to the VPS at `~/.ssh/id_ed25519` before running bootstrap.

- [ ] `python3` with `cryptography` and `secrets` (stdlib) available

**VPS:**

- [ ] Debian 12+ or Ubuntu 22.04+

- [ ] SSH key access configured for the ansible user

- [ ] User has passwordless sudo OR the sudo password is known

- [ ] Ports 22, 80, 443 reachable from the public internet

### 2. Generate Secrets

On your control machine, generate two secrets. Store both in your password manager —
losing the encryption key means losing every tax ID in the registry.

```bash
# Fernet key for encrypting tax IDs at rest (REQUIRED).
export COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY="$(python3 -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# Passphrase for encrypting nightly backup tarballs (REQUIRED, ≥ 16 chars).
export COMPADRE_BACKUP_PASSPHRASE="$(python3 -c \
  'import secrets; print(secrets.token_urlsafe(32))')"
```

### 3. Configure Inventory

```bash
cd deploy/ansible

# Copy the example and edit for your VPS.
cp inventory/production.yml.example inventory/production.yml
```

Edit `inventory/production.yml` and set at minimum:

```yaml
ansible_host: "YOUR_VPS_IP"
ansible_user: "YOUR_SSH_USER"
repo_url: "git@github.com:YOUR_USER/compadre.git"  # or your fork URL
repo_branch: "main"
domain: "YOUR_VPS_IP"  # or real DNS for Let's Encrypt
billing_timezone: "UTC"  # or "America/Sao_Paulo" etc.
```

All other vars have sensible defaults (see the inventory file comments).

### 4. Install Ansible Collections

```bash
ansible-galaxy collection install -r requirements.yml
```

### 5. Verify SSH + Inventory

```bash
# Confirm SSH works.
ssh YOUR_SSH_USER@YOUR_VPS_IP "echo ok && cat /etc/os-release | head -3"

# Confirm inventory parses correctly.
ansible-inventory -i inventory/production.yml --list --yaml | head -20
```

### 6. Run Bootstrap

```bash
ansible-playbook playbooks/bootstrap.yml -i inventory/production.yml \
  -e "ansible_become_password=YOUR_SUDO_PASS"
```

Bootstrap installs Docker, UFW, clones the repo, builds the image, starts the
service, creates cron jobs, and waits for `/health` to report `healthy`.
First-run model download from HuggingFace takes 1–2 minutes on a 100 Mbps link;
the health-check wait loop absorbs this (180 retries × 10 s = 30 min).

**Expected output on success:**

```text
TASK [[Summary] Report deployment status]
============================================
Compadre STT Service — Bootstrap Complete
============================================

Service Status:  healthy
Loaded Models:   large-v3-turbo
============================================
```

### 7. Verify Service

```bash
# Health check.
curl -k https://YOUR_VPS_IP/health
# → {"status": "healthy", "models": ["large-v3-turbo"]}

# Transcription test (after issuing a key — see step 8).
curl -k -X POST \
  -H "Authorization: Bearer <issued-key>" \
  -F "file=@audio.opus" \
  -F "language=pt" \
  https://YOUR_VPS_IP/v1/audio/transcriptions
```

### 8. Issue First API Key

```bash
ssh YOUR_SSH_USER@YOUR_VPS_IP "cd ~/compadre && docker compose exec compadre-stt \
  compadre-keys create-customer \
    --name 'First Customer' \
    --email ops@example.com \
    --cpf 11144477735"
# → cust_01JABC...

ssh YOUR_SSH_USER@YOUR_VPS_IP "cd ~/compadre && docker compose exec compadre-stt \
  compadre-keys issue-key \
    --customer cust_01JABC... \
    --rpm 60 \
    --price 149"
# → cmpd_live_XXXXXXXXXXXXX   (printed once — copy it now)
```

### 9. Run Health Assertions

```bash
ansible-playbook playbooks/health.yml -i inventory/production.yml
echo $?   # 0 = all healthy
```

### 10. Test Backup + Restore Round-Trip

```bash
# Ensure secrets are exported (same as step 2).
# Create a backup.
ansible-playbook playbooks/backup-keys.yml -i inventory/production.yml

# Note the archive path from the summary output (e.g.,
# /var/lib/compadre/registry.db.backup-2026-04-27T20-50-52Z.tar.gz.enc).
# Then restore from it:
ansible-playbook playbooks/restore-keys.yml -i inventory/production.yml \
  -e "ansible_become_password=YOUR_SUDO_PASS" \
  -e "backup_file=/var/lib/compadre/registry.db.backup-<ts>.tar.gz.enc"
```

Both `backup-keys.yml` and `restore-keys.yml` read `COMPADRE_BACKUP_PASSPHRASE`
from the environment. The restore playbook also requires `ansible_become_password`
because it stops/restarts the Docker service.

### Rollback Procedure

If bootstrap fails mid-way, the playbook is idempotent — re-run it:

```bash
ansible-playbook playbooks/bootstrap.yml -i inventory/production.yml \
  -e "ansible_become_password=YOUR_SUDO_PASS"
```

To fully decommission and start over:

```bash
ansible-playbook playbooks/cleanup.yml -i inventory/production.yml \
  -e "ansible_become_password=YOUR_SUDO_PASS"

# Cleanup may leave a stale Docker apt source if the key was removed before
# the source list. Before re-running bootstrap, remove it:
ssh YOUR_USER@YOUR_VPS_IP "echo 'YOUR_SUDO_PASS' | sudo -S rm -f /etc/apt/sources.list.d/docker.list"

# Then re-run bootstrap.
```

### Idempotency Notes

| Playbook | Re-run Safe? | Notes |
|----------|-------------|-------|
| `bootstrap.yml` | Yes | ~5 tasks report `changed` on every re-run (helper container create/remove, docker compose up, cron markers). Core tasks (Docker, UFW, git clone, .env, cron entries) are idempotent. |
| `update.yml` | Yes | The `docker compose up --build` always reports `changed` (fresh build). Git pull is idempotent if no new commits. |
| `deploy-models.yml` | Yes | Helper container tasks report `changed` each run. Model download is a no-op if already cached. |
| `health.yml` | Yes | `changed=0` every run. Read-only, exits 0 on all-green deployment. |
| `backup-keys.yml` | Yes | `changed=1` per run (new archive with different timestamp). Task-level idempotent. |
| `restore-keys.yml` | Yes | `changed=5` per run (stop/start service, helper container lifecycle). DB state is idempotent with same `backup_file`. |
| `cleanup.yml` | Yes | `state: absent` on already-removed artifacts is a no-op. Stale `docker.list` apt source may remain (see Rollback note above). |

---

## Prerequisites

**VPS requirements:**

- 4 vCPU, 8 GB RAM minimum
- Ubuntu 22.04+ or Debian 12+
- SSH key access configured
- Ports: 80 and 443 open (Caddy HTTPS reverse proxy)

**Control machine requirements:**

- Ansible 2.14+ installed
- `community.docker` Ansible collection installed
- SSH access to the VPS
- `COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY` environment variable set (Fernet key — see [Bootstrap](#bootstrap----first-deployment) for generation)
- `COMPADRE_BACKUP_PASSPHRASE` environment variable set (≥ 16 chars, used by backup-keys.yml and restore-keys.yml)

Install the required Ansible collection:

```bash
ansible-galaxy collection install community.docker
```

## Inventory Configuration

Copy `deploy/ansible/inventory/production.yml.example` to `production.yml` and fill in your server details:

```yaml
all:
  hosts:
    compadre_vps:
      ansible_host: "YOUR_VPS_IP"
      ansible_user: "YOUR_SSH_USER"
      ansible_ssh_common_args: "-o ForwardAgent=yes"

  vars:
    key_registry_encryption_key: "{{ lookup('env', 'COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY') }}"
    backup_passphrase: "{{ lookup('env', 'COMPADRE_BACKUP_PASSPHRASE') }}"
    operator_contact_hint: "Contact ops@example.com for billing."
    billing_timezone: "UTC"
    repo_url: "YOUR_REPO_URL"
    repo_branch: "master"
    domain: "localhost"
    landing_enabled: true
    whisper_model_size: "large-v3-turbo"
    whisper_compute_type: "int8"
    whisper_cpu_threads: 4
    whisper_language: "pt"
    hf_home: "/app/models"
```

For Oracle Cloud Ampere A1 (aarch64) deployments, use `deploy/ansible/inventory/oracle.yml.example` as a starting point instead. It configures a real domain for Let's Encrypt certificates.

**Variables:**

| Variable | Required | Description |
| --- | --- | --- |
| `ansible_host` | Yes | VPS IP address or hostname |
| `ansible_user` | Yes | SSH user with key-based auth |
| `key_registry_encryption_key` | Yes | Fernet key for encrypting tax IDs at rest (reads from `COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY` env var on the control machine). Maps to the `KEY_REGISTRY_ENCRYPTION_KEY` env var inside the container. |
| `backup_passphrase` | Yes | Passphrase for encrypting nightly backup tarballs (reads from `COMPADRE_BACKUP_PASSPHRASE` env var on the control machine). Must be ≥ 16 characters. Maps to the `BACKUP_PASSPHRASE` env var inside the container. |
| `operator_contact_hint` | No | Customer-facing contact string echoed in 401 hints. Maps to `OPERATOR_CONTACT_HINT`. |
| `billing_timezone` | No | IANA timezone for billing calculations and the daily suspension sweep day-boundary (default: `UTC`). Maps to `BILLING_TIMEZONE`. **This is a contractual input** — the zone you set here dictates when a customer's `paid_until` expires and their key flips to 401; communicate it formally to customers at contract time. |
| `repo_url` | Yes | Git repository URL |
| `repo_branch` | No | Branch to deploy (default: `master`) |
| `domain` | No | Domain for Caddy HTTPS (default: `localhost` for self-signed). Set to a real domain for Let's Encrypt auto-cert. |
| `landing_enabled` | No | Deploy the public landing page at the root URL (default: `true`). When false, Caddy uses `Caddyfile-no-landing` which proxies all paths to the API. The API endpoints (`/v1/*`, `/health`, etc.) are always available regardless. |
| `whisper_model_size` | No | faster-whisper model size (default: `large-v3-turbo`). |
| `whisper_compute_type` | No | CTranslate2 compute type (default: `int8`). |
| `whisper_cpu_threads` | No | CPU threads for Whisper inference (default: `4`). |
| `whisper_language` | No | Default transcription language (default: `pt`). |
| `hf_home` | No | HuggingFace Hub cache directory (default: `/app/models`). |

## Complete Deployment Flow

A fresh deploy requires two playbooks in sequence:

```bash
cd deploy/ansible
export COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export COMPADRE_BACKUP_PASSPHRASE="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"

# 1. Bootstrap — installs everything, builds and starts services
ansible-playbook playbooks/bootstrap.yml -i inventory/production.yml \
  -e "ansible_become_password=YOUR_SUDO_PASS"

# 2. Verify — read-only diagnostic check
ansible-playbook playbooks/health.yml -i inventory/production.yml

# 3. Register the first customer and issue their API key
ssh YOUR_SSH_USER@YOUR_VPS_IP "cd ~/compadre && docker compose exec compadre-stt \
  compadre-keys create-customer --name 'Acme Demo' --email ops@acme.example --cpf 11144477735"
ssh YOUR_SSH_USER@YOUR_VPS_IP "cd ~/compadre && docker compose exec compadre-stt \
  compadre-keys issue-key --customer cust_<ulid-from-previous-step> --rpm 60 --price 149"
# The plaintext (cmpd_live_...) is printed once; copy it to your client config now.
```

After these three steps, the service is fully operational and ready to authenticate traffic.

## Available Playbooks

| Playbook | Purpose | Requires sudo |
|----------|---------|---------------|
| `bootstrap.yml` | First-time VPS setup (Docker, UFW, build, start) | Yes |
| `deploy-models.yml` | Pre-seed faster-whisper HuggingFace cache for air-gapped deploys | No |
| `update.yml` | Pull latest code, rebuild image, restart services | No |
| `health.yml` | Read-only diagnostic (status, memory, disk, cron) | No |
| `cleanup.yml` | Full decommission: remove all artifacts and system components | Yes |

## Bootstrap -- First Deployment

Deploys Compadre on a clean Ubuntu VPS with a single command.

**What it does:**

1. Verifies the target is Ubuntu/Debian and SSH is working
2. Validates that `KEY_REGISTRY_ENCRYPTION_KEY` and `repo_url` are configured
3. Installs Docker Engine and Docker Compose v2
4. Adds the SSH user to the `docker` group
5. Installs and configures UFW firewall (ports 22, 80, 443)
6. Clones the repository to `~/compadre`
7. Scrubs any legacy `~/compadre/data/keys/` directory left over from pre-volume-migration bootstraps (idempotent on fresh hosts)
8. Creates `.env` with all configuration variables
9. Builds the Docker image and starts the service (compadre-stt + Caddy)
10. Configures cron jobs (daily suspension sweep, nightly key-registry backup)
11. Waits for the health endpoint to report `healthy`

**Bootstrap does NOT create customers or API keys.** Use `compadre-keys create-customer` + `compadre-keys issue-key` to populate the registry before any traffic is expected.

**Run:**

```bash
cd deploy/ansible
# Generate the registry encryption key (one-time).
export COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY="$(python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
# IMPORTANT: Store this value in your password manager. Losing it means losing
# the ability to decrypt every tax ID already in the registry.
ansible-playbook playbooks/bootstrap.yml -i inventory/production.yml \
  -e "ansible_become_password=YOUR_SUDO_PASS"
```

**Expected output on success:**

```text
TASK [[Summary] Report deployment status]
============================================
Compadre STT Service -- Bootstrap Complete
============================================

Service Status:  healthy
Loaded Model:    large-v3-turbo
Endpoint URL:    https://YOUR_VPS_DOMAIN
============================================
```

**Test the deployment (after issuing a key):**

```bash
curl -k -s https://YOUR_VPS_DOMAIN/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_YOUR_PLAINTEXT" \
  -F "file=@test.opus" \
  -F "response_format=text"
```

The playbook is idempotent -- safe to run multiple times.

## Update -- Deploy New Version

Updates a running Compadre deployment from the latest git commit.

**What it does:**

1. Verifies `~/compadre` exists and the service is running
2. Pulls latest changes from git
3. Rebuilds the Docker image and restarts the service
4. Waits for the health endpoint to report `healthy`
5. Reports actionable error if health check fails

**What it preserves:**

- Docker volumes (models, registry) are not affected
- `.env` file is not modified
- Cron jobs remain unchanged

**Run:**

```bash
cd deploy/ansible
ansible-playbook playbooks/update.yml -i inventory/production.yml
```

**If the health check fails after update**, the playbook prints recovery steps:

1. Check logs: `docker compose -f ~/compadre/compose.yaml logs --tail=50`
2. Check status: `docker compose -f ~/compadre/compose.yaml ps`
3. Try restart: `docker compose -f ~/compadre/compose.yaml restart`
4. Roll back: `cd ~/compadre && git log --oneline -5` then `git checkout <previous-commit> && docker compose up -d --build`

## Health -- Diagnostic Report

Read-only diagnostic tool for troubleshooting. Never modifies anything.

**What it reports:**

- Container state (running or not)
- Health endpoint response (status, models, engines, memory, uptime)
- Disk usage (root filesystem)
- Cron job status (daily suspension sweep, nightly key-registry backup)

**Warnings triggered automatically:**

- Memory usage above 5 GB
- Disk usage above 80%
- STT model not loaded

**Run:**

```bash
cd deploy/ansible
ansible-playbook playbooks/health.yml -i inventory/production.yml
```

### Registry-level health — `compadre-keys health`

`ansible-playbook health.yml` covers infrastructure (container state, disk, cron existence at the host level). **Registry-level health** — DB integrity, schema parity, permissions drift, last suspension-sweep age, cron-installed marker, auth-cache telemetry — lives in the `compadre-keys health` CLI verb that runs inside the `compadre-stt` container:

```bash
docker compose exec compadre-stt compadre-keys health              # table output
docker compose exec compadre-stt compadre-keys health --format json  # machine-readable
```

**Exit codes:** `0` healthy, `2` usage error, `5` DB error, `6` configuration error, **`7` unhealthy** (only in `--format json`; table mode always exits `0` — read-tier UX rule). A minimal Nagios-style check:

```bash
set -e
payload="$(docker compose exec -T compadre-stt compadre-keys health --format json)" && rc=$? || rc=$?
case $rc in
  0) echo "OK"; exit 0 ;;
  7) msg="$(echo "$payload" | jq -r '.checks[] | select(.severity == "warn" or .severity == "fail") | "\(.label): \(.value)"' | tr '\n' ';')"
     echo "WARNING: $msg"; exit 1 ;;
  *) echo "CRITICAL: compadre-keys health exited $rc"; exit 2 ;;
esac
```

The checks in each report are: `Database`, `Schema version`, `Customers`, `Keys`, `Last suspension sweep`, `Cron`, `Backup cron`, `TZ drift`, `Last backup`, `Auth cache`.

## Cleanup -- Full Decommission

Removes all Compadre artifacts **and** system-level components from the VPS, restoring it to its pre-bootstrap state. This is irreversible.

**What it removes:**

Application artifacts:

- Docker containers, images, volumes, and build cache
- Cron jobs (`compadre-daily-suspension`, `compadre-keys-backup`, and the `CRON_TZ` envvar; legacy `compadre-daily-restart` is also scrubbed if present)
- `~/compadre` directory

System-level components (installed by bootstrap):

- Docker Engine (`docker-ce`, `docker-ce-cli`, `containerd.io`, `docker-compose-plugin`)
- Docker data directories (`/var/lib/docker`, `/var/lib/containerd`)
- Docker apt repository and GPG key
- Docker group
- UFW firewall (disabled, rules reset, package removed)
- GitHub SSH key from `known_hosts`

**What it keeps:**

- `ca-certificates`, `curl`, `gnupg` — common system utilities, safe to keep

**Run:**

```bash
cd deploy/ansible
ansible-playbook playbooks/cleanup.yml -i inventory/production.yml \
  -e "ansible_become_password=YOUR_SUDO_PASS"
```

After cleanup, the VPS is restored to its pre-bootstrap state. To redeploy, run `bootstrap.yml` again.

## Model Deployment

The faster-whisper model (~1.5 GB, int8 quantized) auto-downloads from HuggingFace Hub on first startup. For air-gapped deployments, pre-seed the cache using the `deploy-models.yml` Ansible playbook.

**Auto-download (default):**

On first startup, the service calls `snapshot_download()` to fetch `Systran/faster-whisper-large-v3-turbo` into `HF_HOME`. This requires outbound HTTPS to `huggingface.co`. Download time is typically 1-2 minutes on a 100 Mbps link.

**Pre-seed for air-gapped deploys:**

```bash
cd deploy/ansible
ansible-playbook playbooks/deploy-models.yml -i inventory/production.yml
```

The playbook creates a temporary helper container with the `compadre-models` volume mounted, copies the pre-downloaded HuggingFace cache into the volume, and cleans up. After deployment, the service skips the download on startup.

**Verify the cache:**

```bash
docker compose exec compadre-stt ls -la /app/models/hub/
```

## HTTPS via Caddy

Compadre uses Caddy as a reverse proxy for automatic HTTPS. The STT service is only exposed internally on the Docker network — all external traffic goes through Caddy on ports 80/443.

**With a public domain (Let's Encrypt):**

Set `DOMAIN` in `.env` to your domain. Caddy automatically provisions and renews certificates:

```bash
DOMAIN=stt.example.com
```

**With a private IP (self-signed):**

Leave `DOMAIN` as `localhost` (default). Caddy generates a self-signed certificate. Clients must accept self-signed certs or use `--insecure`/`-k` with curl.

**Configuration:** The Caddyfile is at `deploy/caddy/Caddyfile`. It uses handle-based routing to serve two concerns from a single domain:

- **API paths** (`/v1/*`, `/health`) are reverse-proxied to `compadre-stt:8000` via Docker internal DNS.

- **All other paths** serve a static landing page from `/srv/www` (the `deploy/landing/` directory bind-mounted in `compose.yaml`).

For multi-replica deployments, the Caddyfile uses the `dynamic a` upstream resolver to re-query Docker DNS every 5 seconds and load-balance across all healthy replica IPs with the `least_conn` policy (queue-depth-aware: routes each request to the replica with the fewest active connections, which equals the fewest queued+in-flight STT jobs in Compadre's single-FIFO-lock architecture). Plain `reverse_proxy host:port` resolves DNS only once at startup and would pin all traffic to a single replica — `dynamic a` is required.

A variant `deploy/caddy/Caddyfile-no-landing` exists for deployments where the landing page is disabled (`landing_enabled: false` in inventory). It proxies all paths to the API with no static file serving. The Ansible playbooks swap in the correct Caddyfile automatically based on the `landing_enabled` variable.

### Landing Page

The default Caddyfile serves a self-contained HTML page at the root URL that lets anyone with a valid API key try Compadre from a browser. Control this with the `landing_enabled` ansible variable:

- **`landing_enabled: true`** (default): Uses the standard `Caddyfile` with handle-based routing. The landing page is served at `/`, API endpoints at their usual paths.

- **`landing_enabled: false`**: Uses `Caddyfile-no-landing`, which proxies all traffic directly to the STT service. Useful for internal/private deployments where a public-facing page is not wanted.

The API endpoints (`/v1/*`, `/health`) are always available regardless of the `landing_enabled` setting.

## Key Registry Management

Compadre authenticates every STT request against a SQLite key registry encrypted with a single Fernet key (`KEY_REGISTRY_ENCRYPTION_KEY`). Customers and their API keys are managed entirely through the `compadre-keys` CLI, which runs inside the compadre-stt container and shares the registry volume.

### How keys work

- The registry file lives at `/var/lib/compadre/registry.db` inside the container (bind-mounted from the `compadre-registry` named volume).
- Each key row stores a SHA-256 hash of the plaintext, an 8-character display prefix, the customer id, `rate_limit_rpm`, `billing_mode`, `price_monthly`, `status`, and `paid_until`.
- Plaintext is a `cmpd_live_<random>` token shown only at issuance (and again after `rotate-key`).
- Authentication is constant-time — unknown tokens, revoked keys, and suspended/past-due keys all return 401 with the same timing profile.

### Initial bootstrap — generate and store the encryption key

```bash
# 1. Generate the Fernet key on your control machine (one-time, per deployment).
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Example output: j8F4...k2Y=

# 2. Store the value in your password manager. Losing it means every tax ID
#    already in the registry is unrecoverable.

# 3. Export it for Ansible and run bootstrap.
export KEY_REGISTRY_ENCRYPTION_KEY="j8F4...k2Y="
ansible-playbook playbooks/bootstrap.yml -i inventory/production.yml \
  -e "ansible_become_password=YOUR_SUDO_PASS"
```

Bootstrap writes `KEY_REGISTRY_ENCRYPTION_KEY` into `~/compadre/.env` on the VPS. The compadre-stt container reads it at startup.

### Seed the first customer and issue a key

```bash
ssh YOUR_SSH_USER@YOUR_VPS_IP "cd ~/compadre && docker compose exec compadre-stt \
  compadre-keys create-customer \
    --name 'Acme Demo' \
    --email ops@acme.example \
    --cpf 11144477735"
# → cust_01jABC...

ssh YOUR_SSH_USER@YOUR_VPS_IP "cd ~/compadre && docker compose exec compadre-stt \
  compadre-keys issue-key \
    --customer cust_01jABC... \
    --rpm 60 \
    --billing-mode manual \
    --price 149"
# → cmpd_live_XXXXXXXXXXXXX   (printed once; paste into the client config now)
```

### Rotating a compromised key

```bash
ssh YOUR_SSH_USER@YOUR_VPS_IP "cd ~/compadre && docker compose exec compadre-stt \
  compadre-keys rotate-key <key-prefix-or-id>"
```

The old plaintext stops working immediately. The `key_id` is preserved, so the per-key rate-limit bucket and all billing state carry over. The new plaintext is printed once — copy it into the client config before the terminal scrolls away. No container restart is required; the registry is read at every cache miss and the cached entry for the old hash simply never matches again.

### Raising or lowering a customer's throughput

```bash
compadre-keys update-key <key-prefix> --rpm 120
```

`--rpm` accepts values in the range 1..10 000. Every `compadre-keys` mutation touches the registry invalidation sentinel, so the change takes effect on the very next request — no cache-TTL wait.

### Retiring a key

```bash
compadre-keys revoke-key <key-prefix> --reason "bundle contract ended 2026-09-30"
```

Revocation is terminal — subsequent requests return 401. Issue a fresh key with `compadre-keys issue-key` if the customer returns.

See [monetization.md](monetization.md) for the full CLI reference and lifecycle reasoning.

### LGPD operator procedures

Brazilian LGPD (Art. 18) gives customers the right to access the personal data Compadre holds about them and to request irreversible deletion. Two CLI verbs implement the operator side of that contract:

**Data-subject access request (Art. 18 II / V).** Emit a machine-readable document with the customer's name, email, decrypted tax ID (formatted), notes, and associated key prefixes:

```bash
compadre-keys export-customer <cust_id> --format json > /tmp/export.json
# or for a human-readable deliverable:
compadre-keys export-customer <cust_id> --format yaml > /tmp/export.yaml
```

The export is non-mutating — no DB rows change, no audit row is written. Forward the resulting document to the subject via the agreed channel (email, client portal, etc.).

**Deletion request (Art. 18 VI).** Permanently purge the customer's personal data and every associated API key. The command deletes all `api_key` rows, scrubs `name` / `email` / `tax_id_encrypted` / `notes` on the customer row, and stamps `deleted_at` — the tombstone row and audit-log history remain to support later ANPD inquiries (FR38 retention contract):

```bash
# Interactive (requires typing "y" at the prompt)
compadre-keys delete-customer <cust_id> --hard

# Scripted (skip the prompt — use in automation)
compadre-keys delete-customer <cust_id> --hard --yes
```

`--hard` is required. After deletion, `list-customers` no longer shows the customer; `list-customers --deleted` shows the tombstone with blank name/email columns as a signal that the row was purged.

### Rotating the encryption key

If `KEY_REGISTRY_ENCRYPTION_KEY` is suspected compromised (leaked `.env`, lost backup tarball, departed contractor with shell history), rotate it without taking the registry offline. The rotation re-encrypts every active customer's `tax_id_encrypted` under the new key in a single transaction — either every row flips or none does (FR36 "no window of unreadable data").

```bash
# 1. Mint a new Fernet key. Keep the value handy for step 3.
#    The -T flag disables TTY allocation — without it Docker may append \r
#    to the captured value and silently corrupt the .env write in step 3.
NEW_KEY="$(docker compose exec -T compadre-stt python -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"

# 2. Rotate (audited; the success line echoes the new key value for the .env swap).
docker compose exec compadre-stt compadre-keys rotate-encryption-key \
  --new-key "$NEW_KEY" --yes

# 3. Update ~/compadre/.env on the host:
#    KEY_REGISTRY_ENCRYPTION_KEY=<value from step 2's success line>

# 4. Restart the service so the new env value is loaded.
docker compose restart compadre-stt

# 5. POST-ROTATION: prune log surfaces that captured the rotation success
#    line. Docker's default ``json-file`` log driver writes container stdout
#    (including the ConfirmationLine echo of the new key) to disk under
#    /var/lib/docker/containers/<id>/. journald similarly persists it. Even
#    with the Compadre app's PII-austere INFO log, the operator-facing echo
#    in step 2's stdout is a real log surface that must be scrubbed.
docker compose logs compadre-stt --since 10m --no-color > /tmp/rotation.log  # inspect
sudo journalctl --rotate && sudo journalctl --vacuum-time=10m  # systemd journal
docker compose down && docker compose up -d  # cycles json-file log files
shred -u /tmp/rotation.log  # if you saved the inspect output
```

Tombstoned customers are skipped by design — their ciphertext is a zero-length BLOB with no PII to rotate. The audit row's `details_json` carries only the structural count (`{"rows_re_encrypted": N}`); the new key value never appears in logs, error panels, or the audit log. The success line is the single operator-facing surface that echoes the value, and it only prints on success.

The full operator handbook — including the breach-notification runbook (ANPD contact, customer-notification template, evidence-preservation checklist, internal escalation path) and the privacy-policy template paragraph — lives at [docs/monetization.md#lgpd-operator-obligations](monetization.md#lgpd-operator-obligations). This section remains the deploy-time operational reference; the handbook is the legal-posture reference.

### Backup & restore

Story 4.3 ships two ansible playbooks — `backup-keys.yml` and `restore-keys.yml` — that together close the 24-hour RPO loop on the key registry. A nightly cron (installed by `bootstrap.yml` at **02:00** in the operator's `billing_timezone`) runs the backup automatically; restore is operator-invoked on demand with an explicit `backup_file` argument. Both playbooks drive the work inside the `compadre-stt` container; no new CLI verb is added.

**One-time setup.** Generate a long passphrase and export it before the first `bootstrap.yml` run:

```bash
export COMPADRE_BACKUP_PASSPHRASE="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"
ansible-playbook deploy/ansible/playbooks/bootstrap.yml \
  -i deploy/ansible/inventory/production.yml \
  -e "ansible_become_password=$SUDO_PASS"
```

Bootstrap asserts the passphrase is ≥ 16 characters, writes it into `~/compadre/.env` as `BACKUP_PASSPHRASE`, and installs the `compadre-keys-backup` cron entry at 02:00 operator TZ. **Store the passphrase in your password manager** — losing it renders every archive unreadable (AES-256-CBC + PBKDF2 with no recovery surface). Every future `bootstrap.yml` run must have the same env var exported.

**Manual backup (ad-hoc).** The cron handles nightly work; invoke manually only before a risky operation (migration, key rotation, `delete-customer --hard`) or to test the path:

```bash
ansible-playbook deploy/ansible/playbooks/backup-keys.yml \
  -i deploy/ansible/inventory/production.yml \
  -e "ansible_become_password=$SUDO_PASS"
```

The archive lands at `/var/lib/compadre/registry.db.backup-<UTC-ISO>.tar.gz.enc` inside the `compadre-registry` named volume at mode `0600`. The summary task prints the archive path + size + retention policy + an SCP hint for off-VPS archival. Archives older than `backup_retention_days` (default **14**) are pruned on every run.

**Restore drill.** The restore is Destroy-tier at the playbook layer (it replaces the live DB file) and requires an explicit `backup_file`. There is no auto-discovery — passing the path is the UX safeguard:

```bash
ansible-playbook deploy/ansible/playbooks/restore-keys.yml \
  -i deploy/ansible/inventory/production.yml \
  -e "ansible_become_password=$SUDO_PASS \
      backup_file=/var/lib/compadre/registry.db.backup-2026-04-20T02-00-00Z.tar.gz.enc"
```

The playbook decrypts + verifies the archive while the service is still running, stops `compadre-stt` briefly (caddy 503s during the window; ≤ 30 s typical), swaps the DB file via a helper alpine container with the registry volume mounted, restarts the service, and polls `/health` until `healthy`. Expected end-to-end wall clock: < 10 minutes for ≤ 10 000 customers (NFR21).

**Archived key ≠ live `.env`.** If the archive was captured under a pre-rotation encryption key and the operator has since run `rotate-encryption-key`, the archived tax-ID ciphertexts only decrypt under the archived key. The restore playbook detects this divergence and prints a `WARN` line with the archived key value. It does NOT write to `.env` — the operator is in the loop for that swap:

1. Edit `~/compadre/.env` — replace `KEY_REGISTRY_ENCRYPTION_KEY` with the archived value printed by the playbook.
2. `docker compose restart compadre-stt` to pick up the new env value.

Until the swap, every `compadre-keys export-customer` call fails with `InvalidToken`.

**Off-VPS archival.** The 14-day on-VPS retention is local belt-and-braces; a single-disk failure loses every archive alongside the DB itself. Rotate archives to cold storage at least weekly via the SCP hint printed by the backup playbook's summary task. Destination is operator-specific (password-manager attachment, encrypted USB, object storage with client-side encryption). Story 4.6 will document the recommended recipe; until then, any of the above is acceptable — the key invariant is "a copy of the archive exists off the VPS".

**Disaster-recovery drill (optional).** To validate the end-to-end round-trip on a disposable second VPS:

1. Bootstrap the drill VPS with the same passphrase, `rsync` a recent archive from the production DB parent directory.
2. `ansible-playbook restore-keys.yml -e "backup_file=/path/to/archive.tar.gz.enc ..."`.
3. Compare `compadre-keys list-customers` against production's latest snapshot.

### Operational health checks — `ansible-playbook health.yml`

Story 4.4 turns `health.yml` into an **assertion playbook**. Every operational invariant becomes an `ansible.builtin.assert` task with a corrective hint, and the playbook exits non-zero on the first deviation. It is read-only — every probe is `changed_when: false`.

```bash
ansible-playbook deploy/ansible/playbooks/health.yml -i deploy/ansible/inventory/production.yml
echo $?   # 0 if healthy, non-zero on any assertion failure
```

The assertion chain (in execution order):

| Assertion | Failure means |
|-----------|---------------|
| DB file mode `0600` (`/var/lib/compadre/registry.db`) | Filesystem perms drifted; run `chmod 600 /var/lib/compadre/registry.db` inside the container. |
| DB parent directory mode `0700` | Parent perms drifted; run `chmod 700 /var/lib/compadre`. |
| `compadre-daily-suspension` cron present in host crontab | The suspension-sweep cron has been removed; re-run `bootstrap.yml`. |
| `compadre-keys-backup` cron present in host crontab | The nightly backup cron has been removed; re-run `bootstrap.yml`. |
| At least one `*.backup-*.tar.gz.enc` archive | No backup has run; either wait for the 02:00 cron or run `backup-keys.yml` manually. |
| UFW active with exactly `22/tcp + 80/tcp + 443/tcp` ALLOW (≤ 6 lines for v4 + v6 dupes) | Firewall rule drifted; review with `sudo ufw status numbered` and remove the unexpected rule. |
| In-container `compadre-keys health --format json` reports `is_healthy=true` | Some DB-state check (schema version, sweep freshness, last-backup age, backup-cron marker, …) flipped to `warn` / `fail`. The fail-message lists each failing row and the corrective hint. |

The `compadre-keys health --format json` exit-7-on-warn/fail contract is the bridge from the in-container CLI to the ansible layer; the playbook does not re-implement DB-state probes.

**Recommended cadences.** Pick whichever matches your existing alerting rhythm — the playbook's value is the exit code, not any specific integration:

- **Nightly cron + log aggregator** — `30 3 * * * cd ~/compadre && ansible-playbook deploy/ansible/playbooks/health.yml -i deploy/ansible/inventory/production.yml 2>&1 | logger -t compadre-health`. Pattern-match on `ASSERTION FAILED` in your log alerting.

- **Per-PR CI** — Story 4.5x will boot a docker-compose-spun-up Compadre and run `health.yml` against it on every pull request; treat the exit code as a build gate.

- **Weekly hands-on audit** — SSH and run the playbook before scheduled maintenance windows. The `success_msg` strings document the green state for the change-control record.

- **Post-incident verification** — the LGPD breach runbook (Story 4.6) will cite `health.yml` as the "after restore-keys.yml, before re-opening to traffic" gate.

**Common failure modes and fixes** (the playbook's own `fail_msg` strings are authoritative — this is the recap):

- **`DB file mode is 600` failed** → `docker compose exec compadre-stt chmod 600 /var/lib/compadre/registry.db`.

- **`compadre-keys-backup cron is installed` failed** → `ansible-playbook bootstrap.yml -i inventory/production.yml -e "ansible_become_password=…"`.

- **`UFW rules match the expected set` failed** → `sudo ufw status numbered && sudo ufw delete <number>` for the unexpected rule.

- **`compadre-keys health is_healthy` failed** → run `docker compose exec compadre-stt compadre-keys health` for the table view; act on whichever row is yellow/red.

## Firewall (UFW)

The bootstrap playbook installs and configures UFW with minimal rules:

| Port | Protocol | Purpose |
|------|----------|---------|
| 22 | TCP | SSH access |
| 80 | TCP | HTTP (Caddy redirects to HTTPS) |
| 443 | TCP | HTTPS (Caddy reverse proxy) |

All other incoming ports are blocked. Outgoing traffic is unrestricted.

**Check status:**

```bash
ssh YOUR_VPS_IP "sudo ufw status"
```

**Temporarily open a port (e.g., for debugging):**

```bash
ssh YOUR_VPS_IP "sudo ufw allow 8080/tcp"
# ... debug ...
ssh YOUR_VPS_IP "sudo ufw delete allow 8080/tcp"
```

## IP Allowlist (Optional)

If the API is only accessed from known IPs (e.g., your n8n server), you can restrict access in the Caddyfile:

```text
{$DOMAIN:localhost} {
    @blocked not remote_ip 203.0.113.10 198.51.100.0/24
    respond @blocked 403

    encode gzip

    @api path /v1/* /health
    handle @api {
        reverse_proxy {
            dynamic a { name compadre-stt; port 8000; refresh 5s }
            lb_policy least_conn
            trusted_proxies private_ranges
        }
    }
    handle {
        root * /srv/www
        file_server
    }

    log {
        output stdout
        format json
    }
}
```

Replace the IPs with your allowed addresses. Restart Caddy after changes: `docker compose restart caddy`.

## Cron Jobs

The bootstrap playbook installs three user-level cron jobs and a `CRON_TZ` envvar. The container's `restart: unless-stopped` policy plus the healthcheck cover transient process failures, so no daily restart is configured.

**Cron entries installed by bootstrap:**

- `compadre-daily-suspension` -- 00:30 in `billing_timezone`, suspends keys whose `paid_until` has elapsed
- `compadre-keys-backup` -- 02:00 in `billing_timezone` (configurable via `backup_hour`), captures an encrypted snapshot of the key registry; archives older than `backup_retention_days` are pruned
- `CRON_TZ` envvar -- pins the cron daemon's timezone to `billing_timezone` so the suspension sweep and backup fire at the configured local hour

Verify cron is active:

```bash
ssh YOUR_SSH_USER@YOUR_VPS_IP "crontab -l | grep compadre"
```

## Horizontal Scaling

Each Compadre instance processes one request at a time (single FIFO queue via `asyncio.Lock`). To increase throughput, add replicas instead of increasing CPU per instance.

### How to scale

Change `replicas` in `compose.yaml` and redeploy:

```yaml
deploy:
  replicas: 2    # or 3, 4, etc.
  resources:
    limits:
      cpus: "4"
      memory: 6G
```

```bash
cd ~/compadre
docker compose up -d --build
```

Caddy load-balances across all healthy replicas using the `dynamic a` upstream resolver in `deploy/caddy/Caddyfile`, which re-queries Docker DNS every 5 seconds and applies the `least_conn` policy. No Caddy configuration changes are needed when adding or removing replicas — only the `replicas` field in `compose.yaml`.

### Resource requirements per replica

| Resource | Per replica | 2 replicas | 3 replicas |
|----------|-----------|------------|------------|
| CPU      | 4 cores   | 8 cores    | 12 cores   |
| RAM      | 6 GB      | 12 GB      | 18 GB      |
| Disk     | Shared (models volume) | Same | Same |

Memory breakdown per replica: Python process with faster-whisper loaded ~2 GB peak RSS. The 6 GB limit provides ~4 GB headroom for concurrent requests and OS buffers.

### What is shared vs per-instance

| Component | Scope | Notes |
|-----------|-------|-------|
| Models (`compadre-models`) | Shared volume | All replicas read the same model files |
| FIFO queue (`asyncio.Lock`) | Per-instance | Each replica has its own queue |
| Per-key rate-limit buckets (`limits` in-memory) | Per-instance | Each replica holds its own moving-window counters. Effective RPM for a customer = `rate_limit_rpm × replicas`. |
| Auth cache (`AuthCache`) | Per-instance | 60 s TTL ceiling; every `compadre-keys` mutation touches `{KEY_REGISTRY_DB_PATH}.invalidate` and each replica's cache flushes on the next read, so a CLI change propagates across replicas synchronously when they share the registry volume. |
| Key registry SQLite | Shared volume (`compadre-registry`) | All replicas read and write the same file. |
| Health endpoint | Per-instance | Reports queue depth and memory for that replica only. |

### Caveats

- **Rate-limit buckets are per-instance.** With N replicas the effective RPM for a key is `N × rate_limit_rpm` because each replica's `limits` in-memory storage runs its own moving window. The default deploy is `replicas: 1`, so this is dormant; if you scale up, either (a) divide the intended per-key RPM by the replica count when setting `compadre-keys update-key --rpm`, or (b) accept the multiplied limit as acceptable elasticity. Shared-storage rate-limiting (Redis/SQLite) is a candidate future story. See [rate-limiting.md](rate-limiting.md).

- **Queue depth is per-instance.** The `/health` endpoint reports the queue depth of whichever replica handles the request. For cluster-wide monitoring, check all replicas or use centralized logging.

- **First deployment with replicas > 1** should start with `replicas: 1` to download and cache models in the shared volume. After models are cached, scale up. Concurrent downloads from multiple replicas to the same temp file can corrupt the model.

- **Caddy blocks on all replicas healthy.** The `depends_on: condition: service_healthy` in compose.yaml means Caddy waits for every replica to pass its healthcheck before starting. If any single replica fails (e.g., OOM during concurrent model loading), Caddy never starts and the entire service is unreachable. For multi-replica deployments, consider changing to `condition: service_started`.
