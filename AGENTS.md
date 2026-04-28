## Project Overview

Compadre is a self-hosted STT API using faster-whisper (large-v3-turbo, int8 quantization, CPU-only). Serves n8n chatbot workflows for WhatsApp/Telegram/Messenger transcription in Brazilian Portuguese.

## Language Policy

- **Everything in English** — code, comments, commits, docs, configs, API responses, error messages, README.md.
- No exceptions. All project files are in English.
- Why: optimizes LLM interaction and developer experience.

## Build & Test

```bash
uv sync                          # Install all deps
uv run pytest                    # Run tests (mocked, no model needed)
uv run pytest -m slow            # Run integration tests (requires model)
uv run ruff check .              # Lint
uv run mypy src/                 # Type check (strict mode)
uv run pre-commit run --all-files  # All quality checks
```

## CI Pipeline

GitHub Actions runs 7 parallel jobs on every push/PR to `master`/`main`. All jobs must pass before merge.

| Job | What it checks | Needs Docker |
|-----|---------------|-------------|
| `lint` | ruff check + ruff format --check + pre-commit (hadolint, yamllint, shellcheck, markdownlint) | No |
| `type-check` | mypy strict mode (`mypy src/`) | No |
| `test-fast` | `pytest -m "not slow and not non_regression"` (~252 tests, mocked, ~27s) | No |
| `test-non-regression` | `pytest -m non_regression tests/test_non_regression/` (architecture contracts, static scans) | Yes |
| `ansible-lint` | ansible-lint + yamllint on `deploy/ansible/` | No |
| `docker-build` | `docker build` (push: false, verify image compiles) | Yes |
| `coverage` | pytest with coverage, `--fail-under=60`, depends on `test-fast` | No |

### Non-Regression Marker Contract

- `@pytest.mark.non_regression` tests MUST live in `tests/test_non_regression/` only. A CI grep step enforces this.
- `pyproject.toml` `addopts` defaults to `-m 'not non_regression'` so developer `pytest` never triggers Docker.
- CI explicitly passes `-m non_regression` for the dedicated job.

### Static Scan Conventions

Non-regression tests enforce architecture contracts via AST/grep scans:

- **No raw HTTPException** — only `CompadreError` subclasses

- **No raw os.environ** — only `config.py` reads env vars (pydantic-settings); `download.py` has a justified exemption for HF_HOME

- **Single Fernet decrypt** — `fernet.decrypt` only in `keys/crypto.py::decrypt_tax_id`

- **Timing-safe compare** — no `==` on secrets

- **No SQL string formatting** — parameterized queries only

Justified exemptions use `# scan-allow[<label>]: <reason>` comments (minimum 3 non-whitespace-char reason). Adding a new scan: register the label in `_scan_common.py::SCAN_LABELS`.

### Reproducing Locally

```bash
bash scripts/ci-local.sh              # all steps
bash scripts/ci-local.sh lint         # single job
bash scripts/ci-local.sh test-fast    # single job
```

Requires: `uv`, `ffmpeg`, optionally `docker`. Full runbook: `docs/ci-runbook.md`.

## Architecture — What You Must Know

- **Single-engine STT**: `POST /v1/audio/transcriptions` accepts `multipart/form-data` with an audio file. faster-whisper large-v3-turbo (CTranslate2 backend, int8 quantized, CPU-only) transcribes the audio. No model selector in MVP — `large-v3-turbo` only.

- **Engine parameters (battle-tested, do not change without load-test validation)**:
  - `beam_size=5`
  - `word_timestamps=True` (enables `verbose_json`, `srt`, `vtt` output formats)
  - `condition_on_previous_text=False` (prevents hallucination cascades on long audio — non-negotiable)
  - `language="pt"` as default; overridable via the `language` form field; auto-detect when omitted
  - Thread count: env-configurable via `COMPADRE_WHISPER_THREADS`, default 4 (matches 4 vCPU reference hardware)

- **Model lifecycle**: large-v3-turbo auto-downloads from HuggingFace on first startup via the CTranslate2 model hub. The model is loaded once at FastAPI lifespan startup and held in a module-level singleton. For air-gapped deployments, pre-seed the HuggingFace cache directory.

- **Single FIFO queue** per instance (`asyncio.Lock`). No multi-worker model. Scale horizontally by increasing `deploy.replicas` in `compose.yaml` — Caddy load-balances across replicas via the `dynamic a` upstream resolver in `Caddyfile` (re-resolves Docker DNS every 5s, `least_conn` policy — queue-depth-aware, exploits the single-FIFO-lock-per-replica design where Caddy's active-connection count equals queued+in-flight requests).

- **Audio preprocessing**: the incoming audio file (OPUS in OGG, MP3, M4A, WAV, WebM) is decoded via `ffmpeg` subprocess to 16 kHz mono PCM s16le WAV in a tempfile before passing to faster-whisper. This is the entire preprocessing stage — no VAD chunking, no noise reduction, no audio enhancement. Limits enforced before engine invocation: max upload 25 MB, max duration 10 minutes (validated via `ffprobe`).

- **Response formatting**: five output formats, all OpenAI-compatible:
  - `text` (default) — plain string, `Content-Type: text/plain`
  - `json` — `{"text": "..."}`
  - `verbose_json` — `{"text", "language", "duration", "segments": [{"start", "end", "text", "words": [{"word", "start", "end", "probability"}]}]}`
  - `srt` — SubRip subtitle format
  - `vtt` — WebVTT subtitle format
  - SRT and VTT formatters reuse the helper from `src/compadre/srt_utils.py` (extracted from a battle-tested implementation).

- **Module boundaries enforced**: no HTTP concerns in `stt.py`, no business logic in `api.py`, no raw `os.environ` (use pydantic-settings), no raw `HTTPException` (use `CompadreError` hierarchy).

- **HTTPS via Caddy**: Caddy reverse proxy terminates TLS (auto Let's Encrypt or self-signed). Caddy is the sole external access point (ports 80/443) — the STT service is not exposed on the host network. Configure domain via `COMPADRE_DOMAIN` env var.

- **Rate limiting**: per-key only via the `limits` library (`MovingWindowRateLimiter` over in-memory storage). `POST /v1/audio/transcriptions` enforces one bucket per API key, keyed on `key:<key_id>` and fed by the registry row's `rate_limit_rpm` (retrieved as `AuthPayload.rate_limit_rpm` during auth). The bucket identity is the DB `key_id`, so it is stable across `compadre-keys rotate-key` (new plaintext, same bucket) and independent for two keys issued to the same customer. `/health` is not rate-limited at the app layer. `get_client_ip()` is retained for the access log (reads `X-Forwarded-For` from Caddy, falls back to `request.client.host`). Rate-limit storage is in-memory per replica — multiplying replicas multiplies effective RPM; documented in `docs/rate-limiting.md`. Per-key counters (`requests_total`, `auth_success`, `rate_limit_429`, `peak_rpm`, `avg_rpm_last_60s`) live in `src/compadre/counters.py`, reset on restart, and are snapshotted every 15 s to `{KEY_REGISTRY_DB_PATH}.counters.{hostname}.json` (one file per replica). `compadre-keys cache-status` and `compadre-keys show <key-prefix> --audit-tail N` glob every matching file and aggregate across replicas — sums for counters, max for `peak_rpm`/`last_activity_at`, min for `snapshot_age_seconds`/`process_uptime_seconds` — so multi-replica deployments see cluster-wide traffic, not last-writer-wins.

- **Landing page**: served by Caddy via `file_server` at root path (`/`). API paths (`/v1/*`, `/health`, `/docs`) routed via `@api` handle block to compadre-stt. Static assets in `deploy/landing/`, bind-mounted as `/srv/www:ro`. Controlled by `landing_enabled` ansible variable (default true). UI is a file upload / drag-and-drop area with API key field, "Transcrever" button, and result text area with copy-to-clipboard. Bring-your-own API key model (same as the prior project's landing page).

- **Import graph flows downward only**: config/logging → exceptions/schemas → preprocessing → service → rate_limit → api/health → main. No circular imports.

## Code Conventions

- Python 3.12, type-annotated (mypy strict + pydantic plugin)
- `ruff` for lint+format (line-length 88)
- All errors via `CompadreError` subclasses with `status_code`, `error_type`, `hint` — never raw HTTPException
- Structured logging: pipe-delimited `timestamp | level | component | message` — never `print()`
- Docker Compose v2 syntax: `docker compose`, never `docker-compose`
- Package management: `uv` only, never `pip` or `poetry`
- Ansible become password: pass via `-e "ansible_become_password=PASSWORD"` on every playbook that uses `become: true` (bootstrap, cleanup). Never modify inventory to store passwords.

## Key File Locations

- `src/compadre/` — application code (config, logging, schemas, exceptions, preprocessing, stt, rate_limit, counters, health, download, api, main)
- `tests/` — pytest suite (mocked, no real models)
- `docs/` — technical and operational docs (API reference, deployment guide, configuration reference, monetization, rate limiting, troubleshooting, API architecture)
- `src/compadre/keys/cli/` — `compadre-keys` CLI (Typer app). Commands: `create-customer`, `list-customers`, `update-customer`, `delete-customer`, `export-customer`, `correct-tax-id`, `issue-key`, `list-keys`, `update-key`, `rotate-key`, `revoke-key`, `rotate-encryption-key`, `mark-paid`, `suspend`, `reactivate`, `at-risk`, `revenue`, `show`, `cache-status`, `suspend-sweep`, `health` (21 verbs total). This is the sole key-management surface.
- `deploy/caddy/Caddyfile` — Caddy reverse proxy config (HTTPS termination)
- `scripts/loadtest.py` — saturation test (concurrency-based)
- `scripts/capacity_test.py` — capacity test (rate-based, WhatsApp context)

## Non-Obvious Gotchas

- **faster-whisper model auto-downloads from HuggingFace** — the model (`Systran/faster-whisper-large-v3-turbo` or equivalent CTranslate2-converted repo) is pulled on first startup. In air-gapped deployments, pre-seed the HuggingFace cache directory (`HF_HOME`) or use `deploy-models.yml`. The download is ~1.5 GB for the int8-quantized CTranslate2 model.

- **`condition_on_previous_text=False` is non-negotiable** — without it, Whisper hallucinates repeated phrases on audio longer than ~30 seconds as it conditions each chunk on the previous output. This parameter has caused production incidents in prior deployments. Do not expose it as a configurable option.

- **ffmpeg must be available in the container** — the Docker image includes `ffmpeg` (already a dependency in the prior project's image for post-processing). The transcription pipeline shells out to `ffmpeg` for audio decoding and `ffprobe` for duration validation. Both must be on `$PATH`.

- **Upload limits are enforced before engine invocation** — 25 MB file size and 10-minute duration max, validated via `ffprobe` on the uploaded file. These limits prevent resource exhaustion from malicious or accidental large uploads. Reject with `CompadreError` subclass (HTTP 413 for size, 422 for duration).

- **large-v3-turbo is the only model in MVP** — the `model` form field is accepted for OpenAI SDK compatibility but ignored. There is no model selector, no model registry, no multi-model support. Do not add model selection in MVP.

- **Word timestamps are always enabled** — `word_timestamps=True` is set at the engine level regardless of the requested `response_format`. The parameter is cheap (CTranslate2 computes timestamps during inference) and is required for `verbose_json`, `srt`, and `vtt` output. The `text` and `json` formats simply discard the timestamp data.

- **Suspension sweep predicate uses strict `paid_until < today`, not `<=`** — a key with `paid_until == today` is considered paid through today; the sweep catches it on the next morning. This matches FR18's "whose paid date has elapsed" wording. Don't "fix" the predicate to `<=` — you'd suspend customers who paid today and haven't yet ridden out their coverage. The cron fires at 00:30 in the operator's configured `billing_timezone` (default **UTC**) via `CRON_TZ` set by `bootstrap.yml`; the app's `BILLING_TIMEZONE` env var MUST match (drift silently shifts the app's "today" relative to the cron schedule). The billing TZ is a **contractual input** — whatever value this holds dictates when `paid_until` expires and a key flips to 401; operators must communicate the zone to customers and avoid changing it mid-relationship. UTC is the operator-neutral international default; override (e.g. `America/Sao_Paulo`) only when you've formally informed the customer base.

- **`compadre-keys health` cannot read the host's crontab from inside the `compadre-stt` container** — cron-installed detection uses a marker file at `{KEY_REGISTRY_DB_PATH}.cron-installed` written by `bootstrap.yml`, not a `crontab -l` shell-out. The marker confirms ansible installed the cron entry; for "is the cron actually firing?" look at the "Last suspension sweep" age field, which reads `audit_log` directly. The two checks complement: marker present + recent sweep = healthy; marker present + stale sweep = cron installed but failing; marker absent = bootstrap not run. Never hand-edit the marker — it will drift from the real crontab and mislead `health`.

- **`compadre-keys health --format json` exits 7 on any warn/fail check**; table mode always exits 0 (read-tier UX rule). Exit codes across `compadre-keys`: 0 success, 2 usage / validation (NUL/CR/LF in `--name` / `--reason`, mutex violations, oversize `--notes`, CPF/CNPJ checksum failure, duplicate tax-ID), 3 entity not found (customer / key prefix), 4 Fernet shape failure (`rotate-encryption-key --new-key` only — every other validation error is exit 2), 5 DB error (sqlite3 / `InvalidToken` on rotation aftermath), 6 config / permission (missing `KEY_REGISTRY_ENCRYPTION_KEY`, unreadable `.env`), 7 unhealthy JSON output. Don't "harmonise" the schema by collapsing 3 into 2 — operators distinguish "you typed a bad CPF" (retry) from "this customer ID doesn't exist" (look it up) via the exit code.

- **LGPD tombstone keeps `tax_id_encrypted BLOB NOT NULL` — purge writes `X''` (zero-length BLOB), not `NULL`, to avoid a v002 migration.** `delete_customer_hard` scrubs `name`/`email`/`notes` to `""` and `tax_id_encrypted` to the zero-length blob so the existing NOT NULL constraints still pass. The canonical tombstone signal is `deleted_at IS NOT NULL`; every read path already treats tombstones as missing. Don't "fix" the schema by dropping the NOT NULL constraints — SQLite has no `ALTER COLUMN DROP NOT NULL`, so the fix would require create-new-table + copy + rename, for zero operator-observable benefit.

- **`export-customer`'s decrypt is the ONLY non-storage-boundary `fernet.decrypt` call in the codebase.** The `rg "fernet\.decrypt" src/` invariant should return exactly one hit — `crypto.py::decrypt_tax_id`. Any new decrypt site must route through `keys.crypto.decrypt_tax_id` (architecture §Security Boundaries: "CPF/CNPJ encryption/decryption ONLY happens in `crypto.py`"). Adding a second Fernet call outside `crypto.py` is a review-blocker.

- **`rotate-encryption-key` re-encrypts only `deleted_at IS NULL` customers**; tombstoned rows hold a zero-length `tax_id_encrypted` BLOB that Fernet would reject (`InvalidToken` on empty input) and that carries no PII to rotate. Don't "fix" the helper to include tombstones — the skip is correct by design (Story 4.1 contract). The audit-row count reflects active rows only. The success ConfirmationLine deliberately echoes the new key value (epic AC verbatim) as a copy-paste hint for the `.env` swap — it is the SOLE surface that prints key material (never in INFO logs, error panels, or the audit `details_json`). Exit-code addendum for `system.py` verbs: 4 joins the table for `rotate-encryption-key`'s `--new-key` Fernet shape check.

- **Backup tarball naming is load-bearing: `{KEY_REGISTRY_DB_PATH}.backup-YYYY-MM-DDTHH-MM-SSZ.tar.gz.enc`.** The glob pattern in `keys/health.py::_check_last_backup` is `*.backup-*.tar.gz.enc` rooted at `Path(KEY_REGISTRY_DB_PATH).parent`. Renaming the archive (dropping `.enc`, moving into a `backups/` subdirectory, switching timestamp format) silently breaks the `Last backup` health row — it reports `never` even with an archive sitting right there. Don't "clean up" the naming; the contract is architecture-reserved since Story 3.4. ISO-8601 uses `-` separators between time components (`T02-00-00Z`, not `T02:00:00Z`) because colons are illegal on Windows and some network filesystems.

- **`BACKUP_PASSPHRASE` is accepted empty at service boot by design (Story 4.3).** `SecretStr` field with `default=SecretStr("")` + a validator that requires `>= 16` chars ONLY when non-empty. Rationale: the passphrase is consumed by the backup/restore playbooks, not by the FastAPI app or any `compadre-keys` CLI verb. Forcing it required-at-boot would make upgrade-to-2.1.0 chicken-and-egg (service won't start → can't run `bootstrap.yml` → service won't start). The ansible playbooks themselves do the strict length check via `ansible.builtin.assert`. Grep invariant: `rg "backup_passphrase" src/compadre/` returns exactly one hit in `config.py`.

- **`restore-keys.yml` warns on archived-key ≠ live `.env` but does NOT auto-swap.** If the archive was captured under a pre-rotation encryption key and the operator has since rotated, the archived `tax_id_encrypted` ciphertexts only decrypt with the archived key. The playbook prints the archived key on stdout as a copy-paste hint for the `.env` swap; it never writes to `.env` itself. Rationale: auto-swapping would silently revert a deliberate rotation; operator-in-the-loop is the safer default. Until the operator swaps `.env` and restarts, every `compadre-keys export-customer` call on a restored customer fails with `InvalidToken`.

- **`ansible-playbook health.yml` is the operational-state assertion playbook (Story 4.4); it exits non-zero on any deviation.** Wire it into monitoring (cron + `logger -t compadre-health`), CI, or operator runbooks. The probes: DB mode `0600`, parent dir mode `0700`, `compadre-daily-suspension` + `compadre-keys-backup` cron entries present in the host crontab, ≥ 1 `*.backup-*.tar.gz.enc` archive in `/var/lib/compadre`, UFW active with exactly 22/80/443 ALLOW (≤ 6 lines for v4 + v6 dupes), in-container `compadre-keys health --format json` reports `is_healthy=true`. Each assertion carries a corrective hint in its `fail_msg`. The pre-existing diagnostic surfaces (Caddy state, `/health` response, disk usage) are preserved as informational pre-checks but the assertion chain is the trustworthy "is the deployment safe?" boolean. Don't soften an assertion to a debug print — operators must be able to `ansible-playbook health.yml; echo $?` and trust the result.

- **`bootstrap.yml`'s explicit chmod tasks (Story 4.4) are belt-and-braces on top of `keys/db.py::get_connection`'s `_chmod_0600` + `mkdir(mode=0o700)`.** The Python connection layer enforces these modes on every connection open — runtime safety net. The ansible tasks make `bootstrap.yml` self-evidently FR42 / NFR13 fulfilling and trace to a single named task (operators don't need to read Python to verify the contract). The `chmod` shell-out goes through `docker compose exec` because the registry path lives inside a named Docker volume — host-side `ansible.builtin.file:` cannot reach the in-container path without `docker volume inspect`-derived host paths, which are docker-engine-internal and break on Docker Desktop / podman. The `[FS] Wait for service lifespan to materialise registry.db` poll (`retries: 30, delay: 2`) is load-bearing — the FastAPI lifespan creates the file on first connection, so the chmod must run AFTER the wait succeeds. Don't "fix" the redundancy by removing either layer — Python protects against runtime drift, ansible protects against bootstrap-time misconfiguration.

- **`compadre-keys health` has TWO cron-installed rows** (Story 4.4): `Cron` (suspension-sweep, marker `{db}.cron-installed`) and `Backup cron` (nightly key-registry backup, marker `{db}.backup-cron-installed`, includes `retention_days`). Both are warn-on-missing and flip `is_healthy=False` independently. The marker paths derive mechanically from `KEY_REGISTRY_DB_PATH`; operators who customise the DB path MUST re-run `bootstrap.yml` so the new marker location matches. The `Backup cron` row sits between `Cron` and `Last backup` in the rendered table — order is documented but not asserted, so JSON consumers iterate the list rather than rely on positional access.

- **`export-customer --include-audit` is opt-in (default off) by design (Story 5.1 Gap 6).** The default export honours the LGPD Art. 18 II/V minimum-necessary reading — name, email, decrypted tax-ID, notes, and key prefixes are the data-subject-facing fields. Adding the audit-log history (every `mark_paid` / `suspend_key` / `rotate_key` / `update_key` event, with actor + timestamp) is a broader-reading interpretation of Art. 18 II/V that operators' counsel may or may not commission. Default-off keeps the lean response correct for the common case; the opt-in is documented in `docs/monetization.md` §1.4. Don't flip the default — counsel-driven, not engineering-driven. Defense-in-depth: the emitted `audit_events[].details` payload re-runs the same `_FORBIDDEN_DETAIL_KEYS` filter that `log_event` enforces at write time (via `audit.py::filter_forbidden_keys`), so a corrupt out-of-band INSERT still cannot leak a PII key through the export.

- **`correct-tax-id` preserves `customer_id` and every `api_key.customer_id` FK; `delete-customer --hard` + `create-customer` does NOT (Story 5.1 Gap 7).** A tax-ID correction (operator typo, CPF↔CNPJ swap, RFB alphanumeric reissue) MUST go through `correct-tax-id` — the helper does an in-transaction UPDATE of `tax_id_encrypted` + `tax_id_kind` and writes an audit row with `{old_kind, new_kind, reason}` (NEVER the value). Delete+recreate produces a tombstone + a brand-new `cust_<ulid>` row; all keys, billing history, and audit chain re-anchor or orphan. The handbook §1.6 carve-out that previously documented the delete+recreate workaround should now point at this verb. The `--reason` validators mirror `revoke-key --reason` (NUL/CR/LF/empty/oversize → exit 2); `--yes` bypasses the Destroy-tier confirmation prompt for scripting. The new tax-ID is encrypted via `crypto.py::encrypt_tax_id` (single-Fernet invariant — `correct-tax-id` does NOT import Fernet).

- **`mark-paid --months` anchors on `paid_until` if it is today-or-later, otherwise on today (Story 5.1 Gap 3).** The "extend the existing window" semantic is operator-friendly when the customer pays before expiry; the "start a new window" semantic kicks in only after expiry so an operator who renews late doesn't lose the days between expiry and renewal. The audit row carries `{"mode": "months", "months": N}` distinguishing this path from `--until`. `--months` and `--until` are mutually exclusive; one is required. Companion flag `--price-monthly` is atomic with the `paid_until` write — single UPDATE statement; the audit row records `{price_monthly_changed: true, old, new}` (price is operator-internal billing data, not PII).

- **`revenue --as-of` is full audit replay (Story 5.1 Gap 8 + review-pass).** `db.py::reconstruct_key_state_at(conn, as_of, *, tz)` walks the `audit_log` forward up to `as_of`, mutating per-key snapshots from `mark_paid` / `suspend_key` / `reactivate_key` / `revoke_key` events. `status` and `paid_until` are fully audit-tracked. `price_monthly` is fully replayable from Story 5.1 review-pass forward: BOTH `mark_paid` (Gap 4) AND `update_key` (review-pass) carry `{price_monthly_changed, old, new}` when the price moves. The replay snapshots each key's price to its current value, then walks every post-`as_of` price-change event and rewinds via the recorded `old` field — so the snapshot at `as_of` is the price right before the next change. `billing_mode` is treated as immutable (no CLI verb mutates it post-`issue_key`). Audit timestamps (`occurred_at` / `created_at`) are converted from UTC to `BILLING_TIMEZONE` before truncating to date so day-boundary comparisons honour the operator's midnight, not UTC's. Validates `earliest_audit_row.date ≤ as_of ≤ today` (both in `BILLING_TIMEZONE`); out-of-range exits 2. **Legacy fallback:** keys whose price moved only via pre-Story-5.1-review audit rows that lack `old/new` will show the current price (the only safe fallback); fresh deployments do not hit this path.

- **`update_customer` audit `details_json` carries `notes_op` (Story 5.1 review-pass).** Two operator paths land in the same `update_customer` action: `--add-note` (additive, append-mode) and `--notes` (destructive, replace-mode). Without a discriminator, a forensic reader cannot detect a notes-column wipe. `notes_op` is `"append"` or `"replace"` (operator-internal metadata, not PII — safe to record). Same-value writes (e.g. `--name "X"` against a customer already named "X") produce no UPDATE and no audit row at the helper level, so the audit log is free of "changed" rows that did not actually change anything. The `_FORBIDDEN_DETAIL_KEYS` invariant is enforced symmetrically on write (now `Sequence`-recursive) and read (`filter_forbidden_keys`).

- **`export-customer --include-audit` envelope grows `audit_events_total` + `audit_events_truncated` (Story 5.1 review-pass).** An LGPD Art. 18 II/V response must be provably complete or provably partial; silently capping at 1000 rows without signalling the truncation is a compliance gap. `audit_events` is now sorted ascending (chronological — matches `monetization.md` §1.4 wording). The `_audit_event_to_export_dict` projection emits `details: {"_corrupt": true}` for rows whose `details_json` is non-decodable or whose top-level JSON is not an object; the previous behaviour (silently dropping the field) hid out-of-band INSERTs from forensic readers. The `_EXPORT_AUDIT_LIMIT=1000` cap is unchanged — operators who hit truncation can paginate via `compadre-keys show --audit-tail` for the slice they need.

## Ansible Deployment (LLM agent instructions)

Full human-oriented guide: [`docs/deployment-guide.md`](docs/deployment-guide.md).

The playbook order for a full deploy is: `cleanup.yml` → `bootstrap.yml` → `deploy-models.yml` → restart service → `health.yml` → `backup-keys.yml`. See the guide's "Complete Deployment Flow" for exact commands per playbook.

### Mandatory invocation pattern for every playbook

```bash
cd /home/galvani/dev/compadre
export COMPADRE_KEY_REGISTRY_ENCRYPTION_KEY="<fernet-key>"    # inventory reads via lookup('env', ...)
export COMPADRE_BACKUP_PASSPHRASE="<passphrase>"              # inventory reads via lookup('env', ...)
ansible-playbook -i deploy/ansible/inventory/production.yml \
  deploy/ansible/playbooks/<PLAYBOOK>.yml \
  -e "ansible_become_password=<sudo-password>"               # required by cleanup, bootstrap, health
```

If either env var is not exported, the playbook fails at the prereq assert.

### LLM-specific friction points (deviations from the human guide)

1. **Always pass `ansible_become_password`** to every playbook. It is required by `cleanup.yml`, `bootstrap.yml`, and `health.yml` (UFW check needs sudo on Debian). It is harmless for the others. The human guide omits it on some examples — ignore that.

2. **After `deploy-models.yml`, restart the service.** The model only loads at startup. Use ad-hoc ansible:
   ```bash
   ansible -i deploy/ansible/inventory/production.yml compadre_vps -m shell \
     -a "docker compose -f ~/compadre/compose.yaml restart compadre-stt"
   ```
   Use `-m shell` (not `-m command`) and pass the command as a plain string without extra quoting.

3. **`compadre-keys create-customer` uses `--cpf` / `--cnpj`, not `--tax-id`.** The bootstrap summary message is outdated. Run `compadre-keys <command> --help` to confirm flags.

4. **`restore-keys.yml` restores DB state from the backup timestamp.** Data created after that backup is lost. If you need to preserve current data, run `backup-keys.yml` first, then restore from that fresh archive.
