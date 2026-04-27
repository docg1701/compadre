# Monetization

Compadre monetization is policy-driven, per API key, with the encrypted SQLite key registry as the single source of truth. There is no global on/off toggle: every request to `POST /v1/audio/transcriptions` is authenticated against the registry and rate-limited by the specific key's `rate_limit_rpm`. The `compadre-keys` CLI is the sole operator surface for creating customers, issuing keys, adjusting limits, and retiring keys.

## What monetization means

Each `api_key` row carries two business-facing fields:

| Field | Type | Purpose |
| --- | --- | --- |
| `rate_limit_rpm` | integer, 1..10 000 | Requests-per-minute allowed for this key. Drives the per-key bucket at request time. |
| `billing_mode` | `manual` or `complimentary` | `manual`: operator-invoiced monthly at `price_monthly` BRL. `complimentary`: bundled externally, no per-key charge, `price_monthly` is always zero. |

`status` (`active` / `suspended` / `revoked`) and `paid_until` gate authentication itself — a suspended or past-due key returns 401 before it ever hits the rate-limit path.

The registry lives at `KEY_REGISTRY_DB_PATH` (default `/var/lib/compadre/registry.db`) and is encrypted at rest via Fernet using `KEY_REGISTRY_ENCRYPTION_KEY`. Tax IDs are ciphertext columns; key plaintexts are never stored (only SHA-256 hashes and an 8-character display prefix).

## Issuing a key

```bash
compadre-keys create-customer --name "Acme" --email ops@acme.example --tax-id 11144477735
# → prints cust_<ulid>
compadre-keys issue-key \
  --customer cust_<ulid> \
  --rpm 60 \
  --billing-mode manual \
  --price 149
```

The plaintext (a `cmpd_live_<random>` token) is rendered once on stdout and never again. `--rpm` must be in the range `1..10 000` (cap enforced by the CLI). For bundled customers where no per-key invoice applies, use `--billing-mode complimentary` and omit `--price`.

## Per-key rate limiting

`POST /v1/audio/transcriptions` runs two dependencies in order: `verify_api_key` (populates `request.state.auth_payload`), then `check_rate_limit` (enforces the per-key bucket).

- **Algorithm**: sliding-window over 1 minute, `limits.aio.strategies.MovingWindowRateLimiter` with in-memory storage.
- **Bucket identity**: `key:<key_id>` — the registry's ULID-based `key_id`, not the plaintext and not the customer_id. Two keys for the same customer each get their own bucket.
- **Rotation stability**: `compadre-keys rotate-key` swaps the plaintext but preserves `key_id`, so the bucket counter carries over. Rotation is not a rate-limit reset.
- **No per-IP bucket**: the application layer does not rate-limit by IP. Reverse-proxy-level hygiene (connection limits, IP blocks) is Caddy's job.

**Multi-replica caveat.** Storage is per-replica in-memory. If `compose.yaml` runs `replicas: N > 1`, effective RPM for a customer is `N × rate_limit_rpm` (each replica enforces the limit independently). The shipped default is `replicas: 1`, so this is dormant today — raise it deliberately and size customer `rpm` values accordingly. The full trade-off is documented in [rate-limiting.md](rate-limiting.md).

## 429 semantics

When a request would exceed the key's per-minute budget, the application raises `RateLimitError` which serializes to:

```text
HTTP/1.1 429 Too Many Requests
Retry-After: 37
X-RateLimit-Limit: 60
X-RateLimit-Remaining: 0
X-RateLimit-Reset: 1745003460
Content-Type: application/json

{
  "error": {
    "message": "Rate limit exceeded",
    "type": "rate_limit_error",
    "hint": "Too many requests. Try again in 37 seconds."
  }
}
```

`Retry-After` and the `Try again in N seconds` hint are derived from `X-RateLimit-Reset - now` — they always match. `X-RateLimit-*` headers are also emitted on allowed responses so clients can observe their remaining budget. The STT engine is never invoked on a 429 — the rate-limit dependency runs before the route handler body.

## Lifecycle operations

| Intent | Command | Effect |
| --- | --- | --- |
| Raise or lower a customer's throughput | `compadre-keys update-key <prefix> --rpm <new>` | New cap is enforced on the next request — no cache-TTL wait. |
| Change monthly price (out-of-cycle) | `compadre-keys update-key <prefix> --price <brl>` | Manual-mode only. Rejected for complimentary keys. Audit row carries `{price_monthly_changed, old, new}` so `revenue --as-of` can rewind it. |
| Confirm payment to an exact date | `compadre-keys mark-paid <prefix> --until YYYY-MM-DD` | Sets `paid_until` to the exact day; auto-reactivates a suspended key. Mutually exclusive with `--months`. |
| Confirm payment for N months | `compadre-keys mark-paid <prefix> --months <1..36>` | Anchors on the existing `paid_until` if it is today-or-later (extends the window), else anchors on today (starts a new window). Month-end clamp applied (Jan 31 + 1 month → Feb 28/29). Mutually exclusive with `--until`. |
| Confirm payment **and** change price atomically | `compadre-keys mark-paid <prefix> --until YYYY-MM-DD --price-monthly <brl>` (or `--months N --price-monthly <brl>`) | Single transaction: `paid_until` and `price_monthly` move together. Audit row records `{price_monthly_changed, old, new}` when the price actually changes. |
| Replace a compromised plaintext | `compadre-keys rotate-key <prefix>` | New plaintext, same `key_id`, same bucket, same billing state. Old plaintext stops working immediately. |
| Retire a key permanently | `compadre-keys revoke-key <prefix> --reason "..."` | Terminal. All subsequent requests with this key return 401. |
| Audit a customer or key | `compadre-keys show <cust_id-or-prefix>` | Detail panel plus the tail of the audit log for that target. |
| Reconstruct historical revenue | `compadre-keys revenue --month YYYY-MM --as-of YYYY-MM-DD` | Replays the audit log up to `--as-of` (in `BILLING_TIMEZONE`) to reproduce status / `paid_until` / `price_monthly` as they stood that day. Validates `earliest_audit_row.date ≤ as_of ≤ today`. |

**Auth cache and synchronous invalidation.** The FastAPI process caches authenticated `AuthPayload` objects (60-second TTL ceiling) keyed by the token hash. Every `compadre-keys` mutation (`update-key`, `revoke-key`, `rotate-key`) touches a sentinel file at `{KEY_REGISTRY_DB_PATH}.invalidate`; the API stats this sentinel on every cache read and flushes immediately when mtime advances. A revoked key stops authenticating on the very next request. The TTL remains a safety net for scenarios where the sentinel cannot be observed (e.g. registry file moved, volume remount).

## What is explicitly out of scope

- **No per-IP fallback.** The application does not rate-limit by source IP. If you need blanket IP hygiene, configure it in Caddy.
- **No single-key environment-variable mode.** Authentication is registry-only. Every bearer token must correspond to an active row in the SQLite registry.
- **No global on/off toggle.** The registry is always authoritative. Issuing a `complimentary` key with a high `rate_limit_rpm` is the way to provision a bypass-style tier.
- **No app-layer `/health` rate limit.** `GET /health` is cheap and idempotent; it returns authenticated-state detail only when a valid bearer is presented.

## LGPD Operator Obligations

Compadre 1.0.0 processes personal data of Brazilian customers — name, email, and encrypted CPF / CNPJ — on your behalf. Under **Lei Geral de Proteção de Dados Pessoais** (Lei 13.709/2018, "LGPD"), **you are the data controller** (Art. 5 VI); Comadre is a software tool. This section is your operator handbook: it enumerates the obligations you carry under LGPD, names the already-shipped Comadre primitive that discharges each one, and provides the runbook you fire on a breach.

The legal basis for processing is *"execução de contrato"* (Art. 7 V) — you collect the data exclusively to execute the API-access subscription. No secondary use, no data sharing, no profiling. The retention period is bounded by the commercial relationship plus the Brazilian commercial-law minimum (typically 5 years for fiscal records). Deletion is operator-driven via `compadre-keys delete-customer --hard`, never automatic.

> **Language note.** The handbook prose is in English (the project's working language; see `CLAUDE.md` §Language Policy). The two notification templates in §Breach-notification runbook are **deliberately Portuguese** because their audiences — ANPD and Brazilian customers — operate in Portuguese. The customer template additionally carries a side-by-side English version as a courtesy for international clientele. This is the sole documented exception to the project-wide English-only rule. **Do not "helpfully" translate the templates to English** — the audience determines the language, not the project convention.

### Controller identification (Art. 5 VI)

You — the operator running Comadre — are the **data controller** under LGPD Art. 5 VI. Comadre is a tool you deploy on your infrastructure to execute that role. The PRD pins this explicitly: *"the operator is the LGPD data controller (Art. 5, VI). Comadre the software is a tool."*.

Two practical consequences flow from that posture:

1. **You publish your own privacy policy.** Comadre ships no privacy policy of its own. The §Privacy-policy template paragraph below is a structural starting point you adapt to your business; it is **not** a complete privacy policy and is **not** legal advice.
2. **ANPD inquiries land on you, never on the Comadre project.** If the Autoridade Nacional de Proteção de Dados (ANPD) opens a procedure under Art. 55-A ff., the named controller is your business — your CNPJ, your responsible person, your contact email. The Comadre project is not a co-controller, not a joint processor, not a respondent.

The architecture predicts this section as the canonical home for the controller declaration. When you hand a privacy notice or compliance attestation to a customer or auditor, point them here.

### Data collected and retention

Comadre's registry stores exactly four customer-level columns. Their purpose, at-rest protection, and retention window are:

| Field | Type | At-rest protection | Retention window |
| --- | --- | --- | --- |
| `name` | free-text | plaintext (low-sensitivity; required for CLI display and operator audit) | commercial-relationship lifetime; purged by `delete-customer --hard` |
| `email` | free-text | plaintext (low-sensitivity; required for CLI display and operator audit) | commercial-relationship lifetime; purged by `delete-customer --hard` |
| `tax_id_encrypted` | CPF or CNPJ | Fernet ciphertext; key material from `KEY_REGISTRY_ENCRYPTION_KEY` | until operator-driven `delete-customer --hard`; purged to zero-length BLOB on tombstone |
| `notes` | free-text | plaintext (operator-facing only) | commercial-relationship lifetime; purged by `delete-customer --hard` |

**Explicit non-collection.** The PRD's data-minimisation clause is binding: *"Data minimization — schema stores only what is necessary…"*. The registry deliberately stores **no address, no phone number, no birthdate, no photograph, no IP, no device fingerprint, no secondary biometrics**. No 2.x story plans to add any of these columns; if a future story does, it carries a corresponding LGPD-impact section in this handbook.

**Encryption-key custody.** You hold `KEY_REGISTRY_ENCRYPTION_KEY` in two places: `~/compadre/.env` on the deploy host (mode `0600`, parent dir `0700`; enforced by `keys/db.py::get_connection` on every connection open and re-asserted by `bootstrap.yml`) and your password manager. **If both copies are lost, every `tax_id_encrypted` ciphertext becomes unreadable.** This is a deliberate fail-closed posture — adding a key-recovery surface would defeat the at-rest-encryption premise. Compare this with `BACKUP_PASSPHRASE`, which is accepted empty at service boot by design (consumed only by the backup/restore playbooks, never by the runtime; see `CLAUDE.md` §Non-Obvious Gotchas — `BACKUP_PASSPHRASE` row): the two secrets sit in different threat models and adopt different boot postures.

**Audit-log retention.** The audit log retains event metadata (who, when, what action, structural counts) but **never** personal data. The invariant is enforced by `src/compadre/keys/audit.py::_FORBIDDEN_DETAIL_KEYS`, which raises `ValueError` if any caller attempts to write a PII-class key (today: `cpf`, `cnpj`, `tax_id`, `name`, `email`, `key_plaintext`, `authorization` — **canonical list at `src/compadre/keys/audit.py`**, the prose enumeration here is illustrative and may drift; treat the source as authoritative) into a `details_json` payload. After a `delete-customer --hard` the customer's PII is gone, but the audit row stays — this satisfies FR38 and your obligation to retain operator-action traceability for ANPD inquiries.

### Handling a data-subject access request (Art. 18 II and V)

LGPD Art. 18 II/V guarantees the data subject the right to confirm processing exists and to access the data held. Comadre's primitive is `compadre-keys export-customer`:

```bash
# 1. Identify the customer from the email / tax-ID they provided in the request.
compadre-keys list-customers  # pick the row whose email matches; note the cust_... ID

# 2. Export in JSON (machine-readable) or YAML (human-readable deliverable).
compadre-keys export-customer cust_01HG7... --format json > /tmp/customer-export.json
compadre-keys export-customer cust_01HG7... --format yaml > /tmp/customer-export.yaml

# 3. Deliver via the agreed channel (signed email reply, client portal, etc.).
#    The export is non-mutating — no DB rows change, no audit row is written.
```

**Field set the export contains**:

- Customer: `id`, `name`, `email`, decrypted `tax_id` (formatted for the kind), `tax_id_kind` (`cpf` or `cnpj`), `notes`, `registered_at`.
- Each associated key (zero or more): `key_prefix`, `status`, `billing_mode`, `created_at`.

**Explicit exclusions.** The export does **not** include:

- The operator-internal **audit log** of actions taken on the customer (default behaviour). Audit rows name the actor and the target; they are operator-forensic records, not subject-access territory under the LGPD minimum-necessary reading. Some legal interpretations of Art. 18 II/V read this scope more broadly — when your counsel commissions that broader interpretation, run `compadre-keys export-customer <cust_id> --include-audit` (Story 5.1 Gap 6). The opt-in appends a chronological-ascending `audit_events` array (oldest first) to the same envelope; each entry carries `action`, `actor`, `occurred_at`, and an optional `details` object filtered through the same forbidden-keys blocklist that `log_event` enforces at write time (no CPF/CNPJ/name/email/key plaintext can surface). The envelope also gains two completeness signals: `audit_events_total` (count of all matching audit rows) and `audit_events_truncated` (boolean — `true` when the array was capped at the 1000-row limit; in that case paginate the slice you need via `compadre-keys show <cust_id> --audit-tail N`). Audit rows whose `details_json` is corrupt or non-object emit `details: {"_corrupt": true}` instead of being silently dropped, so a forensic reader can detect out-of-band INSERTs.
- The **SHA-256 hash** of any API key plaintext. Hashes never leave storage by design; this is a security boundary, not an LGPD posture.
- Operator-internal billing data — `rate_limit_rpm`, `paid_until`, `price_monthly`. These are not subject data under LGPD. You may include them voluntarily by attaching the output of `compadre-keys show cust_... --format json`; that is a customer-relationship choice, not an LGPD-access obligation.

**Statutory deadline.** LGPD Art. 18 §1 sets the response window at *"15 (quinze) dias"*. The `export-customer` command itself completes in well under one second regardless of registry size; the 15-day window is spent on identity-verification administrivia, not on the command.

The deploy-time operational reference for this command lives at `docs/deployment-guide.md:480-504`.

### Handling a deletion request (Art. 18 VI)

LGPD Art. 18 VI is the right to erasure (with the carveouts the law itself names — fiscal records, operator legitimate interest, etc.). Comadre's primitive is `compadre-keys delete-customer --hard`:

```bash
# 1. Confirm the subject's identity (same evidentiary posture as access; the customer's
#    authenticated email plus an independent verification factor is the minimum).

# 2. Interactive run — requires typing "y" at the prompt.
compadre-keys delete-customer cust_01HG7... --hard
# → This will permanently delete customer Marcos Silva and all associated keys
#   (3 key(s)). This action cannot be undone. Encrypted tax ID will be purged.
#   Continue? [y/N]:

# 3. Scripted run (e.g., inside a larger data-governance pipeline) — skip the prompt.
compadre-keys delete-customer cust_01HG7... --hard --yes

# 4. Confirm the tombstone is present.
compadre-keys list-customers --deleted | grep cust_01HG7...
```

**What the command does**. One SQLite transaction:

- `DELETE FROM api_key WHERE customer_id = ?` — every associated key is removed; subsequent requests bearing those plaintexts return 401.
- The customer row is scrubbed in place: `name=""`, `email=""`, `notes=""`, `tax_id_encrypted=X''` (zero-length BLOB; the existing `NOT NULL` constraint stays satisfied). `deleted_at` is stamped UTC.
- One audit row is written for the action — `event_type=delete_customer`, `details_json={"keys_purged": N, "tax_id_kind": "cpf"|"cnpj"}`. Zero PII in the payload (enforced by `_FORBIDDEN_DETAIL_KEYS`).

**Why the audit row stays.** FR38 — *"System preserves audit-log metadata (who, when, what) after a customer's personal data is purged, so operator activity remains traceable without retaining personal data."* The audit log is an append-only forensic record; LGPD does not require destroying operator-action metadata, only the subject's personal data. The audit row links to a tombstoned `customer_id` that no longer resolves to a person — it carries no PII.

**Why the tombstone stays.** The tombstone (`deleted_at IS NOT NULL`, PII scrubbed) is the **evidence of the completed deletion**. If ANPD ever asks "did you delete Marcos Silva's data within the statutory window?", the tombstone is the answer. Every read path filters `deleted_at IS NULL` by default — `list-customers --deleted` is the sole surface that surfaces tombstones (FR7). The zero-length-BLOB choice for `tax_id_encrypted` is deliberate: it satisfies the existing `NOT NULL` constraint without a v002 migration; see `CLAUDE.md` §Non-Obvious Gotchas (LGPD tombstone row) for the rationale.

**Statutory deadline.** Art. 18 §1's 15-day window applies. The command completes in under one second; the budget is spent on identity verification and counsel sign-off, not the command itself.

The deploy-time operational reference for this command lives at `docs/deployment-guide.md:494-504`.

### Handling a correction request (Art. 18 III)

LGPD Art. 18 III is the right to correct incomplete, inaccurate, or out-of-date data. Two procedures depending on which column is affected:

- **Email correction.** Use `compadre-keys update-customer` with `--email`:

  ```bash
  compadre-keys update-customer cust_01HG7... --email new@acme.example
  ```

- **Name correction (legal-name typo, social-name update).** Use `--name` on `update-customer`:

  ```bash
  compadre-keys update-customer cust_01HG7... --name "Acme Holdings Ltda."
  ```

  Preserves `customer_id` and every `api_key.customer_id` FK. Same-value writes (e.g. `--name "X"` against a row already named "X") produce no UPDATE and no audit row.

- **Notes — append vs replace (mutually exclusive).** Two operator paths land in the same `update_customer` audit action; the audit `details_json` carries a `notes_op` discriminator (`"append"` or `"replace"`) so a forensic reader can distinguish a correction-trail entry from a notes-column wipe.

  - `--add-note` appends a single dated line; the audit trail accumulates rather than overwrites:

    ```bash
    compadre-keys update-customer cust_01HG7... \
      --add-note "Razão social corrigida em 2026-04-24: Acme Holdings Ltda."
    ```

  - `--notes` replaces the entire notes column (multi-line allowed). Whitespace-only `--notes ""` is rejected (exit 2) so the operator does not silently wipe the column:

    ```bash
    compadre-keys update-customer cust_01HG7... \
      --notes "rebuilt history\n[2026-04-25] consolidated"
    ```

  See the implementation history of `compadre-keys update-customer` for the verb's contract; the [deploy-guide §Key Registry Management](deployment-guide.md#key-registry-management) has the operational reference.

- **Tax-ID corrections (CPF or CNPJ).** Use `compadre-keys correct-tax-id` (Story 5.1 Gap 7). The verb does an in-transaction UPDATE of `tax_id_encrypted` + `tax_id_kind` and writes one audit row carrying `{old_kind, new_kind, reason}` — **never** the value itself. `customer_id` and every `api_key.customer_id` FK are preserved (no key reissue, no orphaned billing history).

  ```bash
  compadre-keys correct-tax-id cust_01HG7... \
    --new-cpf "111.444.777-35" \
    --reason "operator-typo correction (Story 5.1 Gap 7 audit)"
  ```

  `--new-cpf` and `--new-cnpj` are mutually exclusive; exactly one is required. `--reason` is required, single-line, ≤ 1024 chars; NUL/CR/LF/empty/oversize all exit 2 (mirrors `revoke-key --reason`). The interactive Destroy-tier confirmation prompt can be bypassed with `--yes` for scripting. The new tax-ID is encrypted via `crypto.py::encrypt_tax_id` (the codebase's single Fernet boundary; `correct-tax-id` does not import Fernet directly).

  **Do not use `delete-customer --hard` + `create-customer` for a tax-ID typo** — that path produces a tombstone plus a brand-new `customer_id`; all keys, billing history, and audit chain re-anchor or orphan. The dedicated verb exists precisely to avoid that loss.

### Breach-notification runbook (Art. 48)

LGPD Art. 48 obliges the controller to notify ANPD and the affected data subjects of any security incident that may pose a relevant risk to those subjects. The law specifies *"prazo razoável"* — a reasonable timeframe — without a fixed number of hours. ANPD's 2024 guidance recommends notification "as soon as possible after discovery", typically within two business days for material incidents. **Your operator legal counsel sets your specific notification posture; the runbook below is the operational scaffold.**

The runbook is **self-contained for the `compadre-keys` and `docker compose` operations** — every command you need to fire from the operator shell is in this section. The key-rotation procedure is duplicated here in Lane B rather than linked back to `docs/deployment-guide.md` because link indirection at incident time is a trust failure; the maintenance cost (two surfaces touch when key rotation evolves) is accepted in exchange for incident-day ergonomics. References to `CLAUDE.md` invariants and to specific deploy-guide subsections appear only as background — you do not need to chase them mid-incident, but they are listed for the post-incident review.

#### ANPD contact

The Autoridade Nacional de Proteção de Dados maintains its institutional presence at **`https://www.gov.br/anpd/pt-br`**. The notification channel for security incidents has historically been a sub-page titled *"Comunicação de Incidente de Segurança"* under that domain, plus an institutional email address.

> **Verify the current URL at incident time.** ANPD's web presence has changed historically — sub-paths, form layout, and institutional email have all moved at one point or another. Treat any URL hardcoded in this handbook as **stale by default** and confirm against `https://www.gov.br/anpd/pt-br` at the moment of the incident. Paper mail to ANPD's Brasília address remains accepted as a fallback (look up the current postal address on the same site).

The Template A in §Notification templates below is structured around the field set ANPD's 2024 guidance asks for; the form may have changed by the time you use it. **Have your legal counsel review both the channel and the wording before sending.**

#### Incident-response procedure lanes

Two lanes. Pick the lane that matches the evidence; if both apply, run Lane A (it is the superset).

##### Lane A — "Registry file exposure or suspected SQLite leak"

Triggered when you have evidence — or strong suspicion — that the contents of `/var/lib/compadre/registry.db` (or one of its `.tar.gz.enc` archives in plaintext form) have left your control.

1. **Stop writes.** `docker compose stop compadre-stt` on the VPS. With the upstream down, Caddy returns `502 Bad Gateway` for new requests until the service restarts (`reverse_proxy` does not cache responses; expect monitoring alerts on the 5xx surge — they confirm the freeze took effect). Reads of the archive directory at `/var/lib/compadre/` continue to work.
2. **Snapshot current state for evidence.** Run `ansible-playbook deploy/ansible/playbooks/backup-keys.yml -i deploy/ansible/inventory/production.yml`. This creates a fresh `{registry.db}.backup-YYYY-MM-DDTHH-MM-SSZ.tar.gz.enc` archive at mode `0600` adjacent to the DB. **Do not overwrite the latest nightly archive** — the nightly archive (state-as-of-yesterday) and the incident snapshot (state-at-time-of-incident) are separate evidentiary artefacts; both are listed in the §Evidence-preservation checklist.
3. **Assess scope.** Capture the affected-customer list and per-customer audit tails. Pin one timestamp at the top of the lane so every artefact carries the same incident clock:

   ```bash
   # Pin the incident timestamp ONCE — every artefact captured below reuses it.
   export UTC_STAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)

   docker compose exec -T compadre-stt compadre-keys list-customers --format json \
     > "/tmp/affected-customers.${UTC_STAMP}.json"
   for cust in $(jq -r '.[].id' "/tmp/affected-customers.${UTC_STAMP}.json"); do
     docker compose exec -T compadre-stt compadre-keys show "$cust" --audit-tail 1000 \
       > "/tmp/audit-${cust}.${UTC_STAMP}.log"
   done
   ```

   These two files are the evidentiary baseline for the ANPD notification body and the customer-template fill-in. **`--audit-tail` is capped at 1000 rows by the CLI** (`src/compadre/keys/cli/commands/customers.py`); for high-volume customers exceeding that bound, run the same loop a second time after rotating evidence offsets, or query `audit_log` directly via `sqlite3` against the live DB copy captured in the §Evidence-preservation checklist.
4. **Rotate the encryption key.** Run Lane B steps 1–4 inline (the runbook is self-contained; do not jump out of this lane).
5. **Rotate every affected customer's API key.** For each customer in the affected list:

   ```bash
   docker compose exec compadre-stt compadre-keys rotate-key <prefix>
   ```

   `rotate-key` preserves `key_id` (same rate-limit bucket per `CLAUDE.md` §Non-Obvious Gotchas — the per-key bucket identity row) but issues a fresh plaintext. The leaked registry's key-hashes become stale on the next bucket read; the new plaintext is the only one that authenticates. Capture the new plaintexts in your password manager before delivering them to customers — `rotate-key` echoes the new value once and only once.
6. **Notify your internal escalation path.** Refer to §Internal escalation path. Do not skip this step even if the incident is technically contained; a closed escalation loop is the audit-trail evidence that the post-incident report builds on.
7. **Notify customers.** Use Template B in §Notification templates. Send via the channel the customer registered under (the email column in the customer row).
8. **Notify ANPD.** Use Template A. Send via the channel confirmed in §ANPD contact above. **Counsel reviews the wording before send.**
9. **Restart the service.** `docker compose up -d compadre-stt` — only after the new encryption key is in `~/compadre/.env`, the rotate-encryption-key command has succeeded (Lane B step 2), and at least one rotated customer's `compadre-keys export-customer` round-trips cleanly (post-rotation verification below).
10. **File the post-incident report.** Structured markdown document you retain indefinitely for audit. Recommended fields: discovery timestamp (UTC), occurrence timestamp (UTC, where known), impact category (categories of personal data affected), customer count, evidentiary artefacts captured (cross-reference `incident-<UTC>/` archive name), notification timestamps (ANPD and customer), rotation timestamps (encryption key and per-customer keys), counsel sign-off line, lessons-learned section. **The post-incident report is internal; do not send it to ANPD unless they ask.**

##### Lane B — "Suspected encryption-key compromise (without confirmed SQLite leak)"

Triggered when `KEY_REGISTRY_ENCRYPTION_KEY` itself is suspected exposed (departed contractor with `.env` shell history, leaked backup tarball passphrase, lost laptop with cached env vars) but you have **no** evidence that the registry file content has been exfiltrated.

1. **Generate a new Fernet key and stash it before rotating.** The rotation command's success line is the **only** operator-facing surface that echoes the new value; losing it mid-flow is the worst failure mode.

   ```bash
   # The -T flag disables TTY allocation — without it Docker may append
   # a stray \r to the captured value and silently corrupt the .env write.
   NEW_KEY="$(docker compose exec -T compadre-stt python -c \
     'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
   # Move $NEW_KEY into your password manager NOW — before running step 2.
   ```

2. **Rotate.** One transaction; every active customer's `tax_id_encrypted` re-encrypted under the new key:

   ```bash
   docker compose exec compadre-stt compadre-keys rotate-encryption-key \
     --new-key "$NEW_KEY" --yes
   ```

   Tombstoned customers are skipped by design — their `tax_id_encrypted` is a zero-length BLOB with no PII to rotate (`CLAUDE.md` §Non-Obvious Gotchas — `rotate-encryption-key` tombstone-skip row). The audit row's `details_json` is `{"rows_re_encrypted": N}` — a structural count, no PII. **The success line prints the new key value to stdout** — this is the sole operator-facing surface that echoes it; if you closed the shell without capturing it in step 1, copy it from this line into your password manager **before** continuing.
3. **Update `~/compadre/.env` on the host.** Set `KEY_REGISTRY_ENCRYPTION_KEY=<value>`. Mode stays `0600`; parent dir stays `0700`.
4. **Restart the service.** `docker compose restart compadre-stt` so the new env value loads.
4a. **Scrub log surfaces that captured the rotation success line.** Docker's default `json-file` driver persists container stdout (including the new-key echo) under `/var/lib/docker/containers/<id>/`; journald similarly retains it. Cycle the json-file via `docker compose down && docker compose up -d`, then `sudo journalctl --rotate && sudo journalctl --vacuum-time=10m` to drop the journal slice that captured the rotation. Same scrubbing applies to any `restore-keys.yml` run that emitted an archived-vs-live key divergence WARN: `journalctl -u <ansible-controller-service>` and any `ansible-playbook -vv` capture file must be shredded after the operator finishes the `.env` swap.
5. **Notify your internal escalation path.** ANPD notification for Lane B is a judgement call your counsel makes — a confirmed key compromise without exfiltrated ciphertext may or may not meet the Art. 48 threshold depending on the specific evidence. The handbook does **not** attempt to replace legal counsel; Template A is pre-composed so counsel's review is a one-hour markup pass, not a drafting pass. Customer notification (Template B) is similarly counsel-reviewed; for Lane B you may legitimately conclude that a transparent disclosure at the next regularly-scheduled customer touchpoint is sufficient. Document the decision in the post-incident report either way.

**Post-rotation verification.** Pick a random handful of customers and round-trip an export:

```bash
docker compose exec compadre-stt compadre-keys export-customer <cust_id> --format json | jq -r '.tax_id'
```

A non-empty, well-formed CPF / CNPJ in the output means the new key decrypts old ciphertext — the rotation completed cleanly. A failure (Fernet `InvalidToken`) means the rotation aborted mid-transaction (SQLite's transaction rollback guarantees no partial state); an immediate rerun should succeed. If the immediate rerun also fails, run `ansible-playbook deploy/ansible/playbooks/health.yml -i deploy/ansible/inventory/production.yml` and read the assertion-chain output to diagnose.

#### Notification templates

Both templates are deliberately Portuguese — see the §Language note at the top of this section. Each is a fenced text block with double-brace placeholders for operator-specific values, e.g. `{{operator-name}}` and `{{operator-email}}`. **Both templates carry the `**TEMPLATE — adapt to the specific incident and have your legal counsel review before sending.**` caveat above.**

##### Template A — ANPD notification (Portuguese)

> **TEMPLATE — adapt to the specific incident and have your legal counsel review before sending.**

```text
Para: Autoridade Nacional de Proteção de Dados (ANPD)
Canal: {{anpd-channel-confirmed-at-incident-time}}
Assunto: Comunicação de Incidente de Segurança — {{operator-brand}}
Data da notificação: {{utc-timestamp}}

1. Identificação do controlador
   Razão social: {{operator-legal-name}}
   CNPJ: {{operator-cnpj}}
   Responsável (Encarregado / DPO): {{operator-name}}
   Contato (e-mail e telefone): {{operator-email}} / {{operator-phone}}

2. Natureza do incidente
   Categoria: {{leak-category — vazamento de arquivo SQLite | comprometimento de chave de cifra | acesso interno indevido | outro}}
   Descrição factual (sem especulação): {{factual-description}}

3. Dados afetados
   Categorias: nome, e-mail, CPF/CNPJ criptografado em repouso (Fernet — AES + HMAC-SHA256),
   registro operacional interno (campo livre, não preenchido por padrão).
   Volume: {{customer-count}} titulares.
   {{customer-count-le-10-attachment — se o número de titulares ≤ 10, anexe lista nominal em arquivo cifrado; caso contrário, omitir.}}
   Observação técnica: a coluna tax_id_encrypted armazena ciphertext autenticado; a recuperação dos CPF/CNPJ
   exigiria a chave KEY_REGISTRY_ENCRYPTION_KEY (já rotacionada — vide item 5).

4. Possíveis consequências para os titulares
   {{realistic-impact-assessment — perda de confiança; possibilidade computacionalmente inviável de
   enumeração dos CPF/CNPJ via ataque a Fernet sem a chave; nenhum dado financeiro afetado}}.

5. Medidas adotadas
   - Rotação da chave de cifra (KEY_REGISTRY_ENCRYPTION_KEY) em {{rotation-utc-timestamp}}.
   - Rotação das chaves de API afetadas em {{api-rotation-utc-timestamp}}.
   - Captura forense: snapshot do estado em {{snapshot-utc-timestamp}}, preservado conforme o checklist
     de evidências do operador.
   - Notificação aos titulares enviada em {{customer-notification-utc-timestamp}}.

6. Cronologia
   Data/hora da ocorrência (quando conhecida): {{occurrence-utc-timestamp-or-unknown}}
   Data/hora da descoberta: {{discovery-utc-timestamp}}
   Data/hora desta notificação: {{utc-timestamp}}

7. Próximos passos
   {{ongoing-investigation; relatório pós-incidente em curso; cooperação com a ANPD}}

Atenciosamente,
{{operator-name}}
{{operator-legal-name}} — {{operator-cnpj}}
```

Cite Lei 13.709/2018 Art. 48 §3 (the law's procedural wording) when counsel reviews the final text.

##### Template B — Customer notification (Portuguese + English)

> **TEMPLATE — adapt to the specific incident and have your legal counsel review before sending.**

```text
Assunto: Notificação de incidente de segurança — Comadre / {{operator-brand}}
Subject: Security incident notification — Comadre / {{operator-brand}}

— Português —

Prezado(a) {{customer-name}},

Identificamos em {{discovery-utc-timestamp}} um incidente de segurança envolvendo o sistema que processa
os dados da sua assinatura: nome, e-mail e CPF/CNPJ cifrado. Esta comunicação descreve o que ocorreu, o
que foi afetado e o que estamos fazendo.

O que ocorreu: {{factual-description}}.
Dados afetados: {{data-affected — nome, e-mail, ciphertext do CPF/CNPJ}}.
O que estamos fazendo: {{actions-taken — rotação da chave de cifra; rotação das chaves de API afetadas;
preservação de evidências; notificação à ANPD em {{anpd-notification-utc-timestamp}}}}.
O que pedimos a você: {{customer-action — sua chave de API foi automaticamente substituída; o novo valor
foi enviado em separado por {{channel}}; atualize sua integração até {{deadline}}}}.

Para perguntas, responda este e-mail ou contate {{operator-email}}.

— English —

Dear {{customer-name}},

On {{discovery-utc-timestamp}} we identified a security incident affecting the system that processes
your subscription data: name, email, and encrypted tax ID. This message describes what happened, what
was affected, and what we are doing.

What happened: {{factual-description-english}}.
Data affected: {{data-affected-english — name, email, ciphertext of CPF/CNPJ}}.
What we are doing: {{actions-taken-english — encryption-key rotation; rotation of affected API keys;
evidence preservation; ANPD notification on {{anpd-notification-utc-timestamp}}}}.
What we ask of you: {{customer-action-english — your API key has been automatically replaced; the new
value was sent separately via {{channel}}; please update your integration by {{deadline}}}}.

For questions, reply to this email or contact {{operator-email}}.

{{operator-name}}
{{operator-legal-name}}
```

**Tone rules**: no passive voice; no "we regret to inform" boilerplate; no unverified assurances ("your data has been safe at all times" is **not** a claim you can make mid-incident — make factual claims only). The customer is treated as an adult processing a factual report.

#### Evidence-preservation checklist

Work through this checklist top-to-bottom on the incident day. The list below uses GitHub-style task checkboxes (`- [ ]` → `- [x]` when committed to the post-incident report); inside the report itself, append the UTC timestamp on the same line as a sub-bullet (e.g. `- ✓ 2026-04-24T14:30Z — captured to incident-2026-04-24T14-30-00Z/`). Every captured file lands in a hermetic `incident-${UTC_STAMP}/` directory and is compressed + encrypted with a passphrase stored in your password manager. **`<utc>` placeholders below resolve to `${UTC_STAMP}` from Lane A step 3** — if you reach this checklist outside Lane A, pin it first: `export UTC_STAMP=$(date -u +%Y-%m-%dT%H-%M-%SZ)`.

- [ ] Latest nightly `.tar.gz.enc` archive is intact and at mode `0600` — `ls -l /var/lib/compadre/*.backup-*.tar.gz.enc`. Do **not** modify; this is the state-as-of-yesterday baseline.
- [ ] Incident-time snapshot taken (Lane A step 2 — `ansible-playbook deploy/ansible/playbooks/backup-keys.yml -i deploy/ansible/inventory/production.yml`) — separate artefact from the nightly. Record archive filename.
- [ ] Live DB file captured as-of-incident — `cp /var/lib/compadre/registry.db /tmp/registry.db.incident-${UTC_STAMP}`; then `chmod 0400` to lock the copy read-only.
- [ ] Affected-customer dump — `compadre-keys list-customers --format json > /tmp/affected-customers.${UTC_STAMP}.json`.
- [ ] Per-customer audit tail — `compadre-keys show <cust_id> --audit-tail 1000 > /tmp/audit-<cust_id>.${UTC_STAMP}.log` for every customer in the affected list. The `--audit-tail` flag is capped at 1000 rows by the CLI; for customers exceeding that, query `audit_log` directly via `sqlite3` against the read-only DB copy captured above.
- [ ] Host syslog for the suspected incident window — `journalctl --since '<when>' --until '<when>' > /tmp/host-syslog.${UTC_STAMP}.log`.
- [ ] Docker container logs — `docker compose logs compadre-stt --no-color > /tmp/tts-logs.${UTC_STAMP}.log`. Repeat for `caddy` if the incident touches the reverse proxy.
- [ ] Caddy access logs — copy from the Caddy container. If your Compose version is v2.7+: `docker compose cp caddy:/var/log/caddy/access.log /tmp/caddy-access.${UTC_STAMP}.log`. Older Compose: `docker cp $(docker compose ps -q caddy):/var/log/caddy/access.log /tmp/caddy-access.${UTC_STAMP}.log`. If the access log is configured to write to stdout instead of a file, the previous Docker-logs step covers it.
- [ ] `ansible-playbook health.yml` output at incident time — capture both the assertion-chain exit code AND the full playbook output: `ansible-playbook deploy/ansible/playbooks/health.yml -i deploy/ansible/inventory/production.yml > /tmp/health.${UTC_STAMP}.log; echo "exit=$?" >> /tmp/health.${UTC_STAMP}.log`.
- [ ] UFW status — `sudo ufw status verbose > /tmp/ufw.${UTC_STAMP}.log`. Confirms whether an unexpected port opened.
- [ ] Process list at incident time — `ps auxf > /tmp/ps.${UTC_STAMP}.log`. Confirms whether an attacker process is still running.
- [ ] `.env` file hash — `sha256sum ~/compadre/.env > /tmp/env-hash.${UTC_STAMP}.log` — **hash only**. Never commit `.env` or its plaintext to any artefact bundle.
- [ ] SSH `last` output — `last -n 50 > /tmp/last.${UTC_STAMP}.log`. Records recent shell sessions on the host.

After the items above are checked, build the incident archive. The block below reuses `$UTC_STAMP` (pinned at the top of Lane A step 3); if you reach this checklist via a different path, define it once before running:

```bash
# Reuse the lane-pinned incident timestamp; if not yet set:
: "${UTC_STAMP:=$(date -u +%Y-%m-%dT%H-%M-%SZ)}"

INCIDENT_DIR="/tmp/incident-${UTC_STAMP}"
mkdir -m 0700 "$INCIDENT_DIR"
# All evidentiary captures share the same ${UTC_STAMP} suffix, so this glob is exact.
mv "/tmp/"*."${UTC_STAMP}"* "$INCIDENT_DIR/"

# openssl enc -pbkdf2 prompts for a passphrase on /dev/tty (NOT stdin, which is the tar pipe).
# Have the password-manager entry ready BEFORE running this command — the prompt does not loop.
tar -czf - -C "$(dirname "$INCIDENT_DIR")" "$(basename "$INCIDENT_DIR")" \
  | openssl enc -aes-256-cbc -pbkdf2 -salt -out "${INCIDENT_DIR}.tar.gz.enc"
chmod 0600 "${INCIDENT_DIR}.tar.gz.enc"
```

**Do not upload the incident archive to cloud storage until legal counsel clears it** — the archive contains PII (the affected-customer dump, audit tails, syslog) and qualifies as a regulated artefact. Local-disk + offline-backup is the default posture; cloud upload is an explicit decision counsel signs off on.

#### Internal escalation path

Pre-fill these roles **at deployment time** so the incident-day operator has names, not placeholders. Update on every team change.

- **Primary responder** — `{{operator-name}}` — phone + out-of-band channel. The person who runs the lanes above. Carries the post-incident report to completion.
- **Secondary responder / legal counsel** — `{{operator-legal-counsel}}` — phone + email. Reviews ANPD and customer template wording before send. Sets the notification posture for Lane B.
- **Technical deputy** — `{{operator-technical-deputy}}` — shell access to the VPS + read access to the password manager. Steps in if the primary responder is unavailable. Maintains the rotation runbook in their head.
- **Customer-communications lead** — `{{operator-customer-comms-lead}}` — typically the same as the primary responder for small operators; a separate role for ≥ 10-customer deployments. Owns the customer-template send-out.

**Communications embargo.** No external communication (customers, press, ANPD) before legal counsel signs off on the notification wording. The single exception: if ANPD's statutory clock is about to expire, notify ANPD with the current best-effort text — under-notifying is a violation; over-notifying is not.

### Privacy-policy template paragraph

The block below is a **structural starting point** for the LGPD section of your own privacy policy. Adapt every placeholder; have your counsel review the final text before publishing it on your terms-of-service page. **This template is not legal advice.**

> **Scope of this template.** Drafted for the assumed Comadre 2.0.0 customer profile — Brazilian legal entities (CNPJ-bearing) and natural persons of legal age (CPF-bearing) entering an API-subscription contract. **It does not cover Art. 14 (children and adolescents).** If your customer base may include minors — even as ancillary end-users behind a contracting entity — consult counsel for an Art. 14 addendum (parental consent flow, separate retention rules, narrower processing scope). Comadre's data model has no field that surfaces a minor, but the operator's deployment context determines applicability.

```text
— Português —

Política de Privacidade — Tratamento de Dados Pessoais (LGPD)

Para a prestação do serviço de assinatura de API, {{operator-legal-name}} (CNPJ {{operator-cnpj}}),
doravante "controlador", coleta e trata os seguintes dados pessoais do titular: nome, e-mail e
CPF/CNPJ. O tratamento ocorre por meio do software Comadre (https://github.com/docg1701/compadre),
ferramenta auto-hospedada na infraestrutura do controlador. O CPF/CNPJ é armazenado criptografado em
repouso por meio de algoritmo simétrico autenticado padrão de mercado (Fernet — AES + HMAC-SHA256).
Nenhum outro dado pessoal é coletado: não há endereço,
telefone, data de nascimento, IP, fingerprint de dispositivo nem biometria.

A base legal para o tratamento é a execução de contrato (Lei 13.709/2018, Art. 7, V). O controlador
não compartilha os dados com terceiros, não realiza tratamento secundário e não realiza profiling.

O período de retenção corresponde ao período da relação comercial acrescido do mínimo legal exigido
pela legislação comercial brasileira (tipicamente 5 anos para registros fiscais). A exclusão é
operada pelo controlador a pedido do titular (Art. 18, VI).

O titular tem direito de: confirmar a existência de tratamento e acessar os dados (Art. 18, II e V);
solicitar correção (Art. 18, III); solicitar exclusão (Art. 18, VI). Para exercer estes direitos,
contate {{operator-email}}. O prazo de resposta é de até 15 dias.

Em caso de dúvida ou reclamação, contate primeiro {{operator-email}}. Caso a resposta não seja
satisfatória, o titular pode acionar a Autoridade Nacional de Proteção de Dados (ANPD) em
www.gov.br/anpd.

— English —

Privacy Policy — Personal Data Processing (LGPD)

To deliver the API subscription service, {{operator-legal-name}} (CNPJ {{operator-cnpj}}),
hereinafter "the controller", collects and processes the following personal data of the subject:
name, email, and Brazilian tax ID (CPF/CNPJ). Processing happens through the Comadre software
(https://github.com/docg1701/compadre), a self-hosted tool running on the controller's
infrastructure. The tax ID is stored encrypted at rest using an industry-standard authenticated
symmetric algorithm (Fernet — AES + HMAC-SHA256). No other personal data is collected — no
address, phone, birthdate, IP, device fingerprint, or biometrics.

The legal basis for processing is contract execution (Lei 13.709/2018, Art. 7, V). The controller
does not share data with third parties, perform secondary processing, or perform profiling.

Retention is the duration of the commercial relationship plus the Brazilian commercial-law minimum
(typically 5 years for fiscal records). Deletion is operator-driven on the subject's request
(Art. 18, VI).

The subject has the right to: confirm processing exists and access the data (Art. 18, II and V);
request correction (Art. 18, III); request deletion (Art. 18, VI). To exercise these rights,
contact {{operator-email}}. Response window: up to 15 days.

For questions or complaints, contact {{operator-email}} first. If the response is unsatisfactory,
the subject may escalate to the Autoridade Nacional de Proteção de Dados (ANPD) at
www.gov.br/anpd.
```

**This template is not legal advice.** It is a structural aid drawn from the PRD's binding-requirements list; the operator's counsel adapts it to the specific business posture and reviews it before publishing.

### Deferred work tracked against this handbook

- **Project-commissioned legal-counsel review of the notification templates.** The templates are structural. An operator who commissions their own counsel review is encouraged to PR back any improvements as a structural refinement.
- **Localised Portuguese-primary handbook for Portuguese-native operators.** Today the prose is English (operator-audience); only the templates are Portuguese. A fully-Portuguese variant is a 2.1+ candidate.

> Items previously listed here that were shipped in Story 5.1 — `--include-audit` opt-in on `export-customer` and the dedicated `correct-tax-id` verb — are documented in §Handling a data-subject access request and §Handling a correction request respectively.

### References

- Deploy-time operational reference — `docs/deployment-guide.md:480-528`
- `_FORBIDDEN_DETAIL_KEYS` PII-hygiene guard — `src/compadre/keys/audit.py`
- `encrypt_tax_id` / `decrypt_tax_id` single-site invariant — `src/compadre/keys/crypto.py`
- `delete_customer_hard` single-transaction contract — `src/compadre/keys/db.py`
- `rotate-encryption-key` atomic re-encryption — `src/compadre/keys/cli/commands/system.py`
- `CLAUDE.md` §Non-Obvious Gotchas (tombstone zero-length BLOB; single `fernet.decrypt` site; `rotate-encryption-key` tombstone-skip + key-echo; backup-tarball naming; `BACKUP_PASSPHRASE` empty-at-boot; `restore-keys.yml` archived-key warning; `health.yml` assertion-chain) — `CLAUDE.md`
- `CLAUDE.md` §Language Policy (the Portuguese-template exception's justification) — `CLAUDE.md`
- Lei Geral de Proteção de Dados Pessoais — Lei 13.709/2018 — `https://www.planalto.gov.br/ccivil_03/_ato2015-2018/2018/lei/l13709.htm`
- ANPD (Autoridade Nacional de Proteção de Dados) — `https://www.gov.br/anpd/pt-br` (verify the current notification channel at incident time; see §ANPD contact above)

## See also

- [rate-limiting.md](rate-limiting.md) — algorithm details, bucket semantics, multi-replica behaviour.
- [deployment-guide.md](deployment-guide.md) — bootstrap procedure, encryption-key generation, seeding the first customer.
- [configuration-reference.md](configuration-reference.md) — full environment variable reference.
