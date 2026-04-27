# Implementation Plan 03 — Ansible Playbook Hardening & Fresh-Deploy Validation

## Goal

Make every Ansible playbook work perfectly on a fresh VPS (10.10.10.208) with zero manual intervention. Audit all playbooks, inventory files, scripts, and compose configuration for correctness against the current Compadre STT codebase. Fix every stale TTS reference, env var mismatch, wrong service name, and missing variable. Test idempotency and error handling. Document the exact fresh-deploy runbook.

## Success Criteria

1. `bootstrap.yml` completes on fresh VPS — all phases pass, service healthy
2. `update.yml` completes on bootstrapped VPS — service restarts healthy
3. `deploy-models.yml` correctly pre-seeds Whisper HuggingFace cache for air-gapped deployment
4. `backup-keys.yml` produces valid encrypted `.tar.gz.enc` archive
5. `restore-keys.yml` successfully round-trip restores from that backup
6. `health.yml` all assertions pass on healthy deployment, exits 0
7. `cleanup.yml` removes all Compadre artifacts, exits 0
8. All playbooks are idempotent (second run: zero `changed` tasks)
9. Inventory at `deploy/ansible/inventory/production.yml` correct for 10.10.10.208
10. Complete fresh-deploy runbook in `docs/deployment-guide.md`

## Prerequisites

- **Plan 1 completed** (docs are accurate reference for env vars, endpoints, architecture)
- SSH access to 10.10.10.208 with `ansible_user=galvani`, sudo password available
- `COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY` and `COMPADRE_BACKUP_PASSPHRASE` env vars set on control machine
- Git repo accessible from VPS (if `repo_url` points to GitHub, SSH agent forwarding or deploy key configured)
- `ansible-galaxy collection install -r deploy/ansible/requirements.yml` run on control machine

---

## Audit Findings Summary

Full audit of all 7 playbooks + 3 scripts + 2 inventory files + compose.yaml + Caddyfile against the current codebase revealed these issues:

| # | Severity | File | Issue |
|---|----------|------|-------|
| 1 | **CRITICAL** | `compose.yaml` | Sets `COMPADRE_WHISPER_MODEL_SIZE` etc. as container env vars, but `config.py::Settings` has no `env_prefix` — it looks for `WHISPER_MODEL_SIZE`. Whisper env vars are **never read** by the app; defaults always apply. |
| 2 | **CRITICAL** | `compose.yaml` | `${COMPADRE_HF_HOME}` reads from `.env` but `.env.example` and bootstrap template write `HF_HOME`. Same prefix mismatch: compose substitution uses default, container gets `COMPADRE_HF_HOME` env var, config.py reads `HF_HOME`. |
| 3 | **CRITICAL** | `deploy/caddy/Caddyfile-no-landing` | Reverse proxy upstream name is `tts-service`; should be `compadre-stt`. Caddy would never connect to the STT container with landing page disabled. |
| 4 | **HIGH** | `deploy/ansible/inventory/production.yml` | Contains 9 TTS-specific vars: `default_voice`, `voices_local_dir`, `kokoro_model_url`, `kokoro_voices_url`, `qwen3_enabled`, `qwen3_model_url`, `qwen3_tokenizer_url`, `qwen3_model_local`, `qwen3_tokenizer_local`. Playbooks that reference these (bootstrap.yml, deploy-models.yml) will look for undefined vars or produce wrong output. |
| 5 | **HIGH** | `deploy/ansible/inventory/oracle.yml` | Same TTS vars (9); `qwen3_enabled: false` specifically referenced. |
| 6 | **HIGH** | `deploy/ansible/inventory/production.yml.example` | Same TTS vars. The template operators copy from is wrong. |
| 7 | **HIGH** | `deploy/ansible/inventory/oracle.yml.example` | Same TTS vars. |
| 8 | **HIGH** | `deploy/ansible/playbooks/health.yml` | "Explain Qwen3 voice is missing" task checks for `qwen/` voices — irrelevant for STT. `Voice count` in summary is wrong heading. |
| 9 | **HIGH** | `deploy/ansible/playbooks/bootstrap.yml` | Summary task prints TTS curl example (`kokoro/pm_santa`, `/v1/audio/speech`, JSON body). |
| 10 | **MED** | `deploy/ansible/playbooks/deploy-models.yml` | The alpine helper creates `/models/hub` but doesn't actually pre-seed anything for Whisper. (Note: Qwen3 references were already removed from this file — it is titled "Compadre Model Deployment Playbook" and targets Whisper.) |
| 11 | **MED** | `deploy/ansible/scripts/backup-keys.sh` | Comments reference "tts-service container" (3 instances). |
| 12 | **MED** | `deploy/ansible/scripts/restore-stage.sh` | Comment references "tts-service image". |
| 13 | **MED** | `deploy/ansible/scripts/restore-swap.sh` | Comment references "tts-service is stopped". |
| 14 | **MED** | `deploy/caddy/Caddyfile` | Comment says "API paths → tts-service" (line 100). |
| 15 | **MED** | `deploy/ansible/playbooks/bootstrap.yml` | `compadre-audio-cleanup` cron installed every 6 hours — TTS legacy; STT has no `/audio` volume. Silent no-op forever, misleads operators. |
| 16 | **MED** | `deploy/ansible/playbooks/health.yml` | `[Diag] Check audio volume size` probes `/audio` — TTS legacy; STT temp files go to `/tmp` via Python `tempfile`. |
| 17 | **LOW** | `deploy/ansible/playbooks/bootstrap.yml` | `.env` template writes `DOMAIN={{ domain }}` but compose.yaml reads `${DOMAIN}` — these happen to match, but the template also writes `WHISPER_MODEL_SIZE` which compose maps to `COMPADRE_WHISPER_MODEL_SIZE` (mismatch — see #1). |
| 18 | **LOW** | `deploy/ansible/playbooks/update.yml` | Title and tasks contain "Comadre" in 6 places. Summary line says `Volumes (models, voices, audio, registry) intact` — TTS legacy. |
| 19 | **LOW** | `deploy/ansible/playbooks/cleanup.yml` | Title and tasks contain "Comadre" in 4 places. Cron removal includes `compadre-daily-restart` (legacy TTS cron — harmless but wrong context). Summary line says `(models, voices, audio, caddy-data, caddy-config)` — TTS legacy. |
| 20 | **LOW** | `deploy/ansible/playbooks/backup-keys.yml` | Title and tasks contain "Comadre" in 3 places. Register variable `tts_service_ps` uses stale TTS naming. |
| 21 | **LOW** | `deploy/ansible/playbooks/restore-keys.yml` | Title and tasks contain "Comadre" in 3 places. Register variable `tts_service_ps` uses stale TTS naming. |
| 22 | **LOW** | `deploy/ansible/playbooks/health.yml` | Title and tasks contain "Comadre" in 4 places. Register variable `tts_running_containers` uses stale TTS naming. |
| 23 | **LOW** | `docs/configuration-reference.md` | Env-var table documents `COMPADRE_WHISPER_*` prefixed names but `config.py` has no `env_prefix` — operators will set wrong vars that are silently ignored. |

---

## Task 1: Fix env var name mismatch (compose.yaml ↔ config.py)

The `compose.yaml` env block for `compadre-stt` sets container env vars with `COMPADRE_WHISPER_*` and `COMPADRE_HF_HOME` names. But `config.py::Settings` has no `env_prefix="COMPADRE_"` — it matches field names directly (case-insensitive). `whisper_model_size` matches `WHISPER_MODEL_SIZE`, NOT `COMPADRE_WHISPER_MODEL_SIZE`.

**Decision — Two options:**

- **Option A (minimal):** Change `compose.yaml` to use unprefixed names matching `config.py` and `.env.example`. Also update `bootstrap.yml`'s `.env` template if needed.
- **Option B (consistent):** Add `env_prefix="COMPADRE_"` to `config.py::Settings` (NOT `RegistrySettings`, which would break `KEY_REGISTRY_*` vars). Update `.env.example`, `bootstrap.yml` template, `context.md`, and `docs/configuration-reference.md` to use the `COMPADRE_WHISPER_*` prefix everywhere.

**Recommendation: Option A** — it is the minimal change that fixes the bug without cascading through registry env var names. Only `compose.yaml` needs changing.

- **File**: `compose.yaml`
- **Changes**: In the `compadre-stt.environment` block, change:
  - `COMPADRE_WHISPER_MODEL_SIZE: "${COMPADRE_WHISPER_MODEL_SIZE:-large-v3-turbo}"` → `WHISPER_MODEL_SIZE: "${WHISPER_MODEL_SIZE:-large-v3-turbo}"`
  - `COMPADRE_WHISPER_COMPUTE_TYPE: "${COMPADRE_WHISPER_COMPUTE_TYPE:-int8}"` → `WHISPER_COMPUTE_TYPE: "${WHISPER_COMPUTE_TYPE:-int8}"`
  - `COMPADRE_WHISPER_CPU_THREADS: "${COMPADRE_WHISPER_CPU_THREADS:-4}"` → `WHISPER_CPU_THREADS: "${WHISPER_CPU_THREADS:-4}"`
  - `COMPADRE_WHISPER_LANGUAGE: "${COMPADRE_WHISPER_LANGUAGE:-pt}"` → `WHISPER_LANGUAGE: "${WHISPER_LANGUAGE:-pt}"`
  - `COMPADRE_HF_HOME: "${COMPADRE_HF_HOME:-/app/models}"` → `HF_HOME: "${HF_HOME:-/app/models}"`
- **Acceptance**: After bootstrap, `docker compose exec compadre-stt env | grep WHISPER` shows `WHISPER_MODEL_SIZE`, not `COMPADRE_WHISPER_MODEL_SIZE`. Config loads the correct env var when overridden.

## Task 1b: Fix docs/configuration-reference.md env var names

`docs/configuration-reference.md` documents Whisper env vars with `COMPADRE_WHISPER_*` and `COMPADRE_HF_HOME` prefixed names. Since `config.py::Settings` has no `env_prefix`, these names are **never read** by the app. Operators setting `COMPADRE_WHISPER_MODEL_SIZE` will get the default silently.

- **File**: `docs/configuration-reference.md`
- **Changes**:
  - Rewrite the env-var table to use unprefixed names matching `config.py`:
    - `COMPADRE_WHISPER_MODEL_SIZE` → `WHISPER_MODEL_SIZE`
    - `COMPADRE_WHISPER_COMPUTE_TYPE` → `WHISPER_COMPUTE_TYPE`
    - `COMPADRE_WHISPER_CPU_THREADS` → `WHISPER_CPU_THREADS`
    - `COMPADRE_WHISPER_LANGUAGE` → `WHISPER_LANGUAGE`
    - `COMPADRE_HF_HOME` → `HF_HOME`
  - Update the `.env.example` excerpt code block to show unprefixed names matching the real `.env.example`
  - Update the Docker Compose environment code block to match the corrected `compose.yaml` from Task 1
- **Acceptance**: Every env var listed in the doc exists in `src/compadre/config.py::Settings` and is consumed with its exact unprefixed name. `grep -c "COMPADRE_WHISPER_" docs/configuration-reference.md` returns zero.


## Task 2: Fix Caddyfile-no-landing upstream name

- **File**: `deploy/caddy/Caddyfile-no-landing`
- **Changes**: Line 65: `name tts-service` → `name compadre-stt`
- **Acceptance**: Caddyfile-no-landing reverse_proxy block resolves `compadre-stt` via Docker DNS

## Task 3: Fix Caddyfile comment

- **File**: `deploy/caddy/Caddyfile`
- **Changes**: Line 100: comment `# 5. API paths → tts-service` → `# 5. API paths → compadre-stt`
- **Acceptance**: Comment matches actual service name

## Task 4: Update inventory/production.yml for Compadre STT

- **File**: `deploy/ansible/inventory/production.yml`
- **Changes**:
  - Set `ansible_host: "10.10.10.208"` (fresh deploy target)
  - Set `ansible_user: "galvani"`
  - Change `repo_url` to `"git@github.com:docg1701/compadre.git"` (or correct remote)
  - Change `repo_branch` to `"main"` if appropriate
  - Change `domain: "10.10.10.208"` (self-signed cert, IP-based access for Proxmox LAN)
  - Change `billing_timezone: "America/Sao_Paulo"` (operator choice — update if needed)
  - Add STT-specific vars:
    ```yaml
    whisper_model_size: "large-v3-turbo"
    whisper_compute_type: "int8"
    whisper_cpu_threads: 4
    whisper_language: "pt"
    hf_home: "/app/models"
    ```
  - Remove ALL TTS-specific vars:
    - `default_voice`
    - `voices_local_dir`
    - `kokoro_model_url`
    - `kokoro_voices_url`
    - `qwen3_enabled`
    - `qwen3_model_url`
    - `qwen3_tokenizer_url`
    - `qwen3_model_local`
    - `qwen3_tokenizer_local`
- **Acceptance**: `grep -i 'kokoro\|qwen3\|default_voice\|voices_local_dir' deploy/ansible/inventory/production.yml` returns zero hits

## Task 5: Update inventory/production.yml.example for Compadre STT

- **File**: `deploy/ansible/inventory/production.yml.example`
- **Changes**: Same transformations as Task 4 but keeping `CHANGE_ME` placeholders:
  - Set `ansible_host: "CHANGE_ME"` (unchanged)
  - Set `ansible_user: "CHANGE_ME"` (unchanged)
  - Add commented STT vars with defaults
  - Remove all TTS vars
  - Remove `qwen3_model_local` and `qwen3_tokenizer_local`
  - Update comments to reference STT concepts
  - Change `domain: "localhost"` (keep as default for self-signed)
  - Update `billing_timezone: "UTC"` (keep default; add comment)
- **Acceptance**: Template is self-consistent and free of TTS references

## Task 6: Update oracle.yml and oracle.yml.example

- **File**: `deploy/ansible/inventory/oracle.yml`
- **Changes**: Same removal of TTS vars as Task 4. Add STT vars with appropriate defaults. Keep `domain: "comadre-tts.duckdns.org"` (production domain — operator should update).
- **File**: `deploy/ansible/inventory/oracle.yml.example`
- **Changes**: Same as Task 5 for Oracle template.
- **Acceptance**: Both files are TTS-free and STT-consistent

## Task 7: Fix bootstrap.yml TTS references

- **File**: `deploy/ansible/playbooks/bootstrap.yml`
- **Changes**:
  1. **Summary task** (currently prints `/v1/audio/speech` curl example with `kokoro/pm_santa`):
     Replace with STT example:
     ```bash
     curl -k -X POST \
       -H "Authorization: Bearer <issued-key>" \
       -F "file=@audio.opus" \
       -F "language=pt" \
       https://{{ ansible_host }}/v1/audio/transcriptions
     ```
  2. **`.env` template**: The current template writes vars as `WHISPER_MODEL_SIZE={{ whisper_model_size }}`. Since Task 1 changes compose.yaml to also use unprefixed names, these match. BUT the template writes `DOMAIN={{ domain }}` and compose reads `${DOMAIN}` — these already match. VERIFY template is correct post-Task-1.
  3. **Verify no TTS-specific tasks remain**: Check for any `kokoro`, `qwen`, `espeak`, `voices`, `deploy-voices.yml` references.
  4. **Service name in docker commands**: All use `compadre-stt` — verify.
- **Acceptance**: `grep -i 'kokoro\|qwen\|espeak\|voices\|tts-service' deploy/ansible/playbooks/bootstrap.yml` returns zero hits in task logic (comments referencing TTS as contrast are OK). Summary prints correct STT curl command.

## Task 7b: Remove legacy compadre-audio-cleanup cron from bootstrap.yml

The current `bootstrap.yml` installs a `compadre-audio-cleanup` cron that runs every 6 hours, removing files from `/audio`. The STT `Dockerfile` does **not** create `/audio` — temp files are managed by Python `tempfile` in `/tmp`. This cron silently does nothing forever and misleads operators into thinking there is an audio-artefact lifecycle to manage.

- **File**: `deploy/ansible/playbooks/bootstrap.yml`
- **Changes**:
  1. Remove the `compadre-audio-cleanup` cron installation task
  2. Remove `/audio` volume reference from any related tasks
  3. If the cron is referenced in the summary task, update the summary to not mention it
- **Acceptance**: `grep -c "audio-cleanup\|/audio" deploy/ansible/playbooks/bootstrap.yml` returns zero.


## Task 8: Fix update.yml TTS references

- **File**: `deploy/ansible/playbooks/update.yml`
- **Changes**:
  1. Title comment: `Comadre Update Playbook` → `Compadre Update Playbook`
  2. Description: `Updates a running Comadre deployment` → `Updates a running Compadre deployment`
  3. `[TTS] Rebuild image and restart all services` → `[STT] Rebuild image and restart all services`
  4. Verify all docker compose commands use correct project paths and service names
  5. Verify health check poll expects STT health shape (status, models)
  6. Verify `.env.example` sync logic works with current `.env.example` (Task 1 changed it)
  7. Fix summary line: `Volumes (models, voices, audio, registry) intact` → `Volumes (models, registry) intact`
- **Acceptance**: Playbook is STT-correct and free of TTS references

## Task 9: Rewrite deploy-models.yml for Whisper model cache

The current playbook is a skeleton for Qwen3 GGUF model deployment. Repurpose for faster-whisper HuggingFace cache pre-seeding (air-gapped / slow-link deployments).

This is an iterative task and may take 2+ hours. **Scope to Mode B only for Plan 03** — one-shot download trigger via `docker compose run`. Mode A (pre-seed from operator's local HF cache via SCP) is documented as future work.

- **File**: `deploy/ansible/playbooks/deploy-models.yml`
- **Changes**:
  1. Rewrite title and preamble: describe Whisper model cache download via HuggingFace Hub
  2. **Mode B — Trigger download on VPS**: Run a one-shot `docker compose run` that invokes `python -c "from compadre.download import ensure_whisper_model; ensure_whisper_model()"` to pull the model before the service starts
  3. Update summary task to reference Whisper model, not Qwen3
  4. Document that running this playbook before `bootstrap.yml` means the service starts with the model pre-downloaded (no download delay on first boot)
  5. Remove all Qwen3-specific tasks and descriptions
  6. Keep Mode A (air-gapped pre-seed) as a **commented future-work section** in the playbook
- **Files to inspect**: `src/compadre/download.py` (exact model repo and cache paths)
- **Acceptance**: After running `deploy-models.yml`, `docker compose exec compadre-stt ls /app/models/hub/` shows the model cache directory populated

## Task 10: Fix backup-keys.sh script comments

- **File**: `deploy/ansible/scripts/backup-keys.sh`
- **Changes**:
  - Line 2: `runs INSIDE the tts-service container` → `runs INSIDE the compadre-stt container`
  - Line 25: `must be set inside the tts-service container` → `must be set inside the compadre-stt container`
- **Acceptance**: No "tts-service" references in script

## Task 11: Fix restore-stage.sh and restore-swap.sh script comments

- **File**: `deploy/ansible/scripts/restore-stage.sh`
- **Changes**: Line 39: `tts-service image` → `compadre-stt image`
- **File**: `deploy/ansible/scripts/restore-swap.sh`
- **Changes**: Line 3: `tts-service is stopped` → `compadre-stt is stopped`
- **Acceptance**: No "tts-service" references in either script

## Task 12: Fix health.yml TTS diagnostics

- **File**: `deploy/ansible/playbooks/health.yml`
- **Changes**:
  1. Title: `Comadre Operational Health Assertion Playbook` → `Compadre Operational Health Assertion Playbook`
  2. Remove `[Diag] Explain Qwen3 voice is missing` task entirely (checks for `qwen/` voices — irrelevant for STT)
  3. Summary task: change `Voice count:` to `Model count:`; update the field reference from `hc.voices` to `hc.models`
  4. Verify all assertion paths use correct `key_registry_db_path` (all derive from inventory var — verify they work)
  5. Verify `[Diag] Caddy container state` uses correct container name
  6. Verify `[Diag] Query /health endpoint` expects `models` not `voices` in health response
  7. Remove `[Diag] Check audio volume size` task entirely — STT has no `/audio` volume; temp files go to `/tmp` via Python `tempfile`. Also remove the `Verify audio cleanup cron is active` hint from the disk-usage warning message
- **Acceptance**: `health.yml` passes all assertions on a healthy STT deployment; `grep -i 'qwen\|kokoro\|voice\|audio volume\|audio cleanup' deploy/ansible/playbooks/health.yml` returns zero hits in task logic

## Task 13: Fix cleanup.yml TTS references

- **File**: `deploy/ansible/playbooks/cleanup.yml`
- **Changes**:
  1. Title: `Comadre Cleanup Playbook — Full Decommission` → `Compadre Cleanup Playbook`
  2. `[TTS] Stop and remove containers...` → `[STT] Stop and remove containers...`
  3. Remove `compadre-daily-restart` cron removal (legacy TTS cron — doesn't exist for STT)
  4. Remove `compadre-audio-cleanup` cron removal (STT has no audio cleanup cron — see Task 7b)
  5. Verify cron names match what bootstrap installs: `compadre-daily-suspension`, `compadre-keys-backup`
  6. Verify summary references correct service name
  7. Fix summary line: `Docker volumes removed (models, voices, audio, caddy-data, caddy-config)` → `Docker volumes removed (models, registry, caddy-data, caddy-config)`
- **Acceptance**: Cleanup removes all Compadre artifacts; `grep -i 'qwen\|kokoro\|voice\|audio' deploy/ansible/playbooks/cleanup.yml` returns zero hits

## Task 14: Test bootstrap.yml on 10.10.10.208

**Pre-flight:**
```bash
# 1. Generate keys
export COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY="$(python -c \
  'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
export COMPADRE_BACKUP_PASSPHRASE="$(python -c \
  'import secrets; print(secrets.token_urlsafe(32))')"

# 2. Verify SSH
ssh galvani@10.10.10.208 "echo ok && cat /etc/os-release"

# 3. Verify inventory
cd /home/galvani/dev/compadre/deploy/ansible
ansible-inventory -i inventory/production.yml --list --yaml | head -30

# 4. Install collections
ansible-galaxy collection install -r requirements.yml
```

**Run:**
```bash
ansible-playbook playbooks/bootstrap.yml -i inventory/production.yml \
  -e "ansible_become_password=YOUR_SUDO_PASS"
```

- **Acceptance**: Bootstrap completes all phases with exit 0. Health check passes. `curl -k https://10.10.10.208/health` returns `{"status": "healthy", "models": ["large-v3-turbo"]}`

**If bootstrap fails**, iterate on the specific task that failed. Common failure modes:
- Docker GPG key URL wrong for Debian distribution → adjust `ansible_facts['distribution']` logic
- UFW `community.general.ufw` module missing → `ansible-galaxy collection install community.general`
- `~/compadre/.env` not readable by docker compose → check file perms
- Service health check timeout (model download on first start) → the `start_period: 300s` in `compose.yaml` covers this, but verify

## Task 15: Test all remaining playbooks sequentially

After successful bootstrap:

1. **Issue a test key** via `docker compose exec compadre-stt compadre-keys create-customer` + `issue-key`
2. **Test transcription** via curl with multipart upload
3. **Run `backup-keys.yml`**: verify archive produced, mode 0600
4. **Run `health.yml`**: verify all assertions pass, exit 0
5. **Run `deploy-models.yml`**: verify idempotent (model already cached)
6. **Run `update.yml`**: verify service restarts healthy
7. **Run `restore-keys.yml`**: with the backup from step 3, verify round-trip restore
8. **Run `cleanup.yml`**: verify all artifacts removed
9. **Re-run `bootstrap.yml`**: verify clean re-deploy from post-cleanup state

- **Acceptance**: Every playbook succeeds with exit 0

## Task 16: Test idempotency

For each playbook, run twice consecutively. Verify:
- Second run has `changed=0` (or changes that are truly idempotent — e.g., `backup-keys.yml` produces a new archive each run, which is correct behavior)
- No `failed` tasks on either run
- `health.yml` always exits 0 when deployment is healthy

- **Acceptance**: Idempotency documented per playbook in the runbook

## Task 17: Test failure modes

| Playbook | Failure Scenario | Expected Outcome |
|----------|-----------------|------------------|
| `bootstrap.yml` | `COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY` not set | Fails at `[Prereq] Validate key registry encryption key` with actionable message |
| `bootstrap.yml` | `COMPADRE_BACKUP_PASSPHRASE` not set or < 16 chars | Fails at `[Prereq] Validate backup passphrase` |
| `bootstrap.yml` | `repo_url` still `CHANGE_ME` | Fails at `[Prereq] Validate repository URL` |
| `restore-keys.yml` | `backup_file` not passed via `-e` | Fails at `[Prereq] Validate backup_file argument` with usage hint |
| `restore-keys.yml` | `backup_file` points to nonexistent path | Fails at `[Prereq] Verify backup archive is readable` |
| `restore-keys.yml` | Wrong `COMPADRE_BACKUP_PASSPHRASE` | Fails during decryption with clear error |
| `health.yml` | DB file mode changed to 0644 | Assertion fails with `chmod 600` corrective hint |
| `health.yml` | Cron entry removed | Assertion fails with `re-run bootstrap.yml` hint |

- **Acceptance**: All failure modes produce clear, actionable error messages. No unhandled exceptions or tracebacks.

## Task 18: Document the fresh-deploy runbook

- **File**: `docs/deployment-guide.md`
- **Changes**: Add a "Fresh Deploy Runbook" section as the first major section after the title, structured as:
  1. **Prerequisites checklist** (control machine + VPS)
  2. **Step 1: Generate secrets** (encryption key + backup passphrase)
  3. **Step 2: Configure inventory** (copy `production.yml.example`, edit vars)
  4. **Step 3: Install Ansible collections**
  5. **Step 4: Run bootstrap** (exact command with env var exports)
  6. **Step 5: Verify service** (curl health + transcription test)
  7. **Step 6: Issue first API key** (create-customer + issue-key)
  8. **Step 7: Run health assertions**
  9. **Step 8: Test backup + restore round-trip**
  10. **Rollback procedure** (if bootstrap fails mid-way)
  11. **Idempotency notes** (which playbooks can be re-run safely)

- **Acceptance**: A new operator can deploy Compadre by following the runbook copy-paste, without reading any other docs

---

## Files to Modify

| File | Changes |
|------|---------|
| `compose.yaml` | Fix Whisper env var names (remove `COMPADRE_` prefix from container env vars) |
| `deploy/caddy/Caddyfile-no-landing` | Fix upstream service name `tts-service` → `compadre-stt` |
| `deploy/caddy/Caddyfile` | Fix comment "tts-service" → "compadre-stt" |
| `deploy/ansible/inventory/production.yml` | Set VPS IP, remove TTS vars, add STT vars |
| `deploy/ansible/inventory/production.yml.example` | Remove TTS vars, add STT vars with comments |
| `deploy/ansible/inventory/oracle.yml` | Remove TTS vars, add STT vars |
| `deploy/ansible/inventory/oracle.yml.example` | Remove TTS vars, add STT vars |
| `deploy/ansible/playbooks/bootstrap.yml` | Fix summary curl example, verify .env template |
| `deploy/ansible/playbooks/update.yml` | Fix "Comadre" branding in title/comments/task names |
| `deploy/ansible/playbooks/deploy-models.yml` | Rewrite for Whisper HuggingFace cache pre-seeding |
| `deploy/ansible/playbooks/health.yml` | Remove Qwen3 diagnostic, fix "Comadre" branding, rename `tts_running_containers` → `compadre_containers`, update summary |
| `deploy/ansible/playbooks/cleanup.yml` | Fix "Comadre" branding, remove legacy cron, verify cron names |
| `deploy/ansible/playbooks/backup-keys.yml` | Fix "Comadre" branding, rename `tts_service_ps` → `compadre_service_ps` |
| `deploy/ansible/playbooks/restore-keys.yml` | Fix "Comadre" branding, rename `tts_service_ps` → `compadre_service_ps` |
| `deploy/ansible/scripts/backup-keys.sh` | Fix "tts-service" → "compadre-stt" in comments |
| `deploy/ansible/scripts/restore-stage.sh` | Fix "tts-service" → "compadre-stt" in comments |
| `deploy/ansible/scripts/restore-swap.sh` | Fix "tts-service" → "compadre-stt" in comments |
| `docs/deployment-guide.md` | Add fresh-deploy runbook, update TTS references found during audit |
| `docs/configuration-reference.md` | Update env var names to match actual config.py (Plan 1 scope — cross-reference) |

## New Files

None expected.

## Dependencies

- **Plan 1** must be complete or in progress (docs accurate for env var names, endpoints, architecture). If not started, Tasks 4-5 (inventory vars) and Task 18 (runbook) will need to be self-contained.
- SSH access to 10.10.10.208 must be verified before Task 14
- Ports 22, 80, 443 must be reachable on 10.10.10.208 (bootstrap configures UFW, but initial SSH must work)
- `ansible-galaxy` must have network access to install `community.docker` and `community.general` collections

## Risks

| Risk | Impact | Mitigation |
|------|--------|-----------|
| **Env var prefix decision (Task 1)** | If wrong, Whisper config is silently ignored. | Option A (remove prefix from compose.yaml) is minimal and aligned with config.py's current behavior. Document why in commit message. |
| **VPS uses Debian 13 (trixie)** | Docker apt repo URL may differ from Ubuntu; package names may change. | `bootstrap.yml` uses `ansible_facts['distribution']` for Docker repo URL — test and adjust if needed. |
| **Model download ~1.5 GB on first startup** | `start_period: 300s` in compose healthcheck covers this, but slow links may exceed timeout. | `bootstrap.yml` wait loop has 180 retries × 10s = 30 min. Adequate. |
| **`restore-keys.yml` service stop window** | Brief 503 from Caddy during DB swap. | Document expected downtime (~30s) in runbook. Verify playbook reports it. |
| **Production inventory carries sensitive IPs** | `production.yml` has real VPS IP (10.10.10.207). New target is 10.10.10.208. | Task 4 updates the IP. Ensure old IP not committed. |
| **`deploy-models.yml` rewrite complexity** | Pre-seeding HuggingFace cache correctly requires knowing the exact directory structure faster-whisper expects. | Inspect `download.py` and `huggingface_hub` cache layout before writing. Test locally first. |
| **UFW `community.general.ufw` module may need Python `ufw` library on target** | Module may fail if `ufw` Python bindings not available. | bootstrap installs `ufw` apt package (includes Python library). Verify. |
| **Caddyfile-no-landing may have been tested with TTS service name** | If no operator has used `landing_enabled: false`, this path is untested. | Task 2 fixes the name. Test this code path on fresh deploy. |

## Validation Checklist

- [ ] `compose.yaml` Whisper env vars match config.py field names (Task 1)
- [ ] `Caddyfile-no-landing` upstream is `compadre-stt` (Task 2)
- [ ] `inventory/production.yml` has zero TTS vars (Task 4)
- [ ] `inventory/production.yml.example` has zero TTS vars (Task 5)
- [ ] `inventory/oracle.yml` and `.example` have zero TTS vars (Task 6)
- [ ] `bootstrap.yml` summary prints correct STT curl command (Task 7)
- [ ] `update.yml` is free of "Comadre" references (Task 8)
- [ ] `deploy-models.yml` correctly pre-seeds Whisper cache (Task 9)
- [ ] `backup-keys.sh`, `restore-stage.sh`, `restore-swap.sh` comments fixed (Tasks 10-11)
- [ ] `health.yml` has zero Qwen3/kokoro/voice references in task logic and zero "Comadre" references (Task 12)
- [ ] `cleanup.yml` is free of "Comadre" references (Task 13)
- [ ] `backup-keys.yml` is free of "Comadre" references and stale `tts_*` variable names (Task 16)
- [ ] `restore-keys.yml` is free of "Comadre" references and stale `tts_*` variable names (Task 16)
- [ ] `bootstrap.yml` completes on 10.10.10.208 with exit 0 (Task 14)
- [ ] `curl -k https://10.10.10.208/health` returns `{"status": "healthy", "models": ["large-v3-turbo"]}` (Task 14)
- [ ] Transcription endpoint works with multipart upload (Task 15)
- [ ] `backup-keys.yml` produces valid `.tar.gz.enc` archive (Task 15)
- [ ] `restore-keys.yml` round-trip restore succeeds (Task 15)
- [ ] `health.yml` exits 0 with all assertions passed (Task 15)
- [ ] `update.yml` restarts service healthy (Task 15)
- [ ] `cleanup.yml` exits 0; re-bootstrap succeeds (Task 15)
- [ ] All playbooks are idempotent (Task 16)
- [ ] All failure modes produce actionable error messages (Task 17)
- [ ] Fresh-deploy runbook documented and tested (Task 18)
