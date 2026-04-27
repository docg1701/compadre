# Implementation Plan 04 — CLI Key Management Exhaustive Testing

## Goal

Test every `compadre-keys` CLI verb (21 total) on the real VPS (`10.10.10.208`)
via SSH, covering every edge case, every exit code (0–7), and every
architecture-level invariant. Verify the complete key management lifecycle —
customer creation through billing, suspension, LGPD deletion, encryption
rotation, and audit replay — with state verification via direct SQLite queries.

## Success Criteria

1.  All 21 CLI verbs execute without crashing and produce correct output
2.  Every exit code (0, 2, 3, 4, 5, 6, 7) is triggered by at least one real
    condition and verified
3.  CPF/CNPJ validation: valid passes, checksum-failure → exit 2, duplicate
    (same tax-ID lookup) → exit 2
4.  Key lifecycle (issue → use → rotate → revoke) works end-to-end;
    rotation preserves `key_id`, rate-limit bucket, and billing state
5.  `mark-paid --months` anchoring: extend from `paid_until` when
    today-or-later, from `today` when expired; `--until` exact-date works;
    `--price-monthly` is atomic with `paid_until` write
6.  `correct-tax-id` preserves `customer_id` and all `api_key.customer_id`
    FK references; `delete-customer --hard` + `create-customer` does NOT
    preserve identity (the anti-pattern)
7.  `delete-customer --hard` tombstone contract: `tax_id_encrypted = X''`
    (zero-length BLOB, NOT NULL), `name/email/notes = ""`, `deleted_at` set,
    `tax_id_lookup = NULL`, all `api_key` rows DELETEd
8.  `export-customer --include-audit` is opt-in (default off), chronological
    ascending, capped at 1000, signals `audit_events_total` and
    `audit_events_truncated`
9.  `revenue --as-of` replays audit log to reconstruct historical key state
    correctly (status, `paid_until`, `price_monthly`)
10. `rotate-encryption-key` re-encrypts ONLY `deleted_at IS NULL` customers;
    tombstones are skipped; round-trip verify (rotate → restart with new key
    → `export-customer` decrypts successfully)
11. `suspend-sweep --dry-run` preview matches `suspend-sweep` execution;
    sweep uses strict `paid_until < today` (not ≤); complimentary keys and
    `paid_until IS NULL` rows are never touched
12. `health --format json` exits 7 on any `warn` or `fail` severity check;
    `health --format table` always exits 0
13. `cache-status` aggregates across per-replica counter snapshots
14. All invariants verified: single-Fernet-call, no raw `HTTPException`, no
    raw `os.environ`, backup naming, audit forbidden keys, tombstone shape,
    exit-code table, import graph

## Prerequisites

- **Plan 3 completed:** `bootstrap.yml` succeeded on `10.10.10.208`;
  Compadre service is running and `/health` returns 200
- SSH access: `ssh galvani@10.10.10.208` works without password prompt
- `docker compose exec compadre-stt` works; registry initialized
- A disposable test environment (no production customer data at risk)
- `sqlite3` available in the container (`python:3.12-slim` includes it)
- The following env vars sourced on the remote host or passed inline:

```bash
# On the VPS, after SSH:
cd ~/compadre
# Registry encryption key (must match .env):
export KREK="$(grep KEY_REGISTRY_ENCRYPTION_KEY .env | cut -d= -f2)"
```

## SSH Convenience Alias

Throughout this plan, `CE` means "docker compose exec compadre-stt":

```bash
CE="docker compose exec -T compadre-stt"
# For interactive commands (rotate-key confirm prompt, etc.):
CEI="docker compose exec compadre-stt"
```

When a command needs `-it` (TTY), use `CEI`; for non-interactive output
capture, use `CE`.

---

## Task 1 — Preparation: Setup Test Harness and Clean Registry Snapshot

**Goal:** Open a SQLite shell to the registry and capture the baseline state
before any mutations. Create a helper script for repeated verification.

### Step 1.1 — Open interactive SQLite and verify schema

```bash
ssh galvani@10.10.10.208
cd ~/compadre
$CE sqlite3 /var/lib/compadre/registry.db
```

Inside sqlite3, confirm the expected tables and indexes:

```sql
.tables
-- Expected: api_key  audit_log  customer  schema_version

.schema customer
.schema api_key
.schema audit_log

SELECT version, applied_at FROM schema_version;
-- Expected: 3 | <ISO timestamp>

SELECT COUNT(*) FROM customer;
SELECT COUNT(*) FROM api_key;
SELECT COUNT(*) FROM audit_log;
-- Should all return 0 on a fresh registry
```

Exit: `.quit`

### Step 1.2 — Create a reusable verification script

On the VPS, create a file for repeated SQL verification queries:

```bash
cat > ~/verify-registry.sh << 'VERIFY_EOF'
#!/bin/bash
# verify-registry.sh — run direct SQLite checks against the registry
set -e
CE="docker compose exec -T compadre-stt"
DB="/var/lib/compadre/registry.db"

echo "=== Customers ==="
$CE sqlite3 "$DB" "SELECT id, name, email, tax_id_kind, length(tax_id_encrypted), notes, registered_at, deleted_at FROM customer;"

echo "=== API Keys ==="
$CE sqlite3 "$DB" "SELECT id, customer_id, key_prefix, status, billing_mode, rate_limit_rpm, price_monthly, paid_until, created_at FROM api_key;"

echo "=== Audit Log (last 20) ==="
$CE sqlite3 "$DB" "SELECT id, occurred_at, actor, action, target_type, target_id, details_json FROM audit_log ORDER BY id DESC LIMIT 20;"

echo "=== Counts ==="
$CE sqlite3 "$DB" "SELECT 'customers' AS tbl, COUNT(*) FROM customer UNION ALL SELECT 'api_keys', COUNT(*) FROM api_key UNION ALL SELECT 'audit_rows', COUNT(*) FROM audit_log;"

echo "=== Tombsones ==="
$CE sqlite3 "$DB" "SELECT id, name, length(tax_id_encrypted), deleted_at FROM customer WHERE deleted_at IS NOT NULL;"
VERIFY_EOF

chmod +x ~/verify-registry.sh
```

### Step 1.3 — Sanity-check the service

```bash
# Minimal health check
curl -k https://10.10.10.208/health
# Expected: {"status":"healthy","models":["large-v3-turbo"]}

# CLI health
$CE compadre-keys health
# Expected: green table, all checks ok/info, no warns/fails

$CE compadre-keys health --format json
echo "Exit: $?"
# Expected: Exit 0 (healthy)
```

---

## Task 2 — Customer Verbs: create-customer, list-customers, update-customer, show

**Goal:** Test customer CRUD with all edge cases, validating CPF/CNPJ,
controls, edits, and the two note modes (append vs replace).

### 2.1 — `create-customer`: happy paths

```bash
# Valid CPF (with punctuation — normalized internally)
$CE compadre-keys create-customer \
  --name "Helena Ribeiro" \
  --email "helena@clinica.com" \
  --cpf "111.444.777-35" \
  --notes "5 bots, WhatsApp booking"
# Expected: exit 0, prints "Customer created: cust_<26-char-ulid> (Helena Ribeiro)"

# Capture the customer ID from output
C1=$(echo "..." | ...) # or read from verify-registry.sh

# Valid CNPJ
$CE compadre-keys create-customer \
  --name "Clinica São Paulo Ltda" \
  --email "contato@clinica-sp.com.br" \
  --cnpj "11.222.333/0001-81" \
  --notes "Enterprise plan"
# Expected: exit 0
```

**State verification (SQLite):**

```sql
-- Verify both customers inserted, tax_id_encrypted non-empty
SELECT id, name, email, tax_id_kind, length(tax_id_encrypted), notes
FROM customer WHERE deleted_at IS NULL;
-- Expected: 2 rows, tax_id_encrypted length > 0 (Fernet ciphertext)

-- Verify audit rows written
SELECT action, target_type, target_id FROM audit_log WHERE action='create_customer';
-- Expected: 2 rows

-- Verify tax_id_lookup populated (v3)
SELECT id, length(tax_id_lookup) FROM customer WHERE deleted_at IS NULL;
-- Expected: both have 32-byte lookup blobs
```

### 2.2 — `create-customer`: edge cases (exit 2)

```bash
# Invalid CPF (checksum fail)
$CE compadre-keys create-customer --name "Bad" --email b@b.com --cpf "111.444.777-36"
echo "Exit: $?"
# Expected: exit 2, "CPF inválido" or similar

# Duplicate tax-ID (same CPF as Helena above)
$CE compadre-keys create-customer --name "Dup" --email d@d.com --cpf "11144477735"
echo "Exit: $?"
# Expected: exit 2, "already registered" message (DuplicateTaxIdError)

# NUL byte in name
$CE compadre-keys create-customer --name $'Test\x00Bad' --email t@t.com --cpf "11144477735"
echo "Exit: $?"
# Expected: exit 2, "illegal control characters"

# Newline in name
$CE compadre-keys create-customer --name $'Test\nBad' --email t@t.com --cpf "11144477735"
echo "Exit: $?"
# Expected: exit 2

# Whitespace-only name
$CE compadre-keys create-customer --name "   " --email t@t.com --cpf "11144477735"
echo "Exit: $?"
# Expected: exit 2

# Empty email
$CE compadre-keys create-customer --name "OK" --email "" --cpf "11144477735"
echo "Exit: $?"
# Expected: exit 2 (pydantic email validation)

# Oversized notes (>8192 chars, the update-customer replace-mode cap)
HUGE_NOTES=$(python3 -c "print('x'*8193)")
$CE compadre-keys create-customer --name "OK" --email t@t.com --cpf "11144477735" --notes "$HUGE_NOTES"
echo "Exit: $?"
# Expected: exit 2

# Mutex violation: --cpf and --cnpj both provided
$CE compadre-keys create-customer --name "OK" --email t@t.com --cpf "11144477735" --cnpj "11222333000181"
echo "Exit: $?"
# Expected: exit 2

# Neither --cpf nor --cnpj
$CE compadre-keys create-customer --name "OK" --email t@t.com
echo "Exit: $?"
# Expected: exit 2
```

### 2.3 — `list-customers`

```bash
# Default (active only)
$CE compadre-keys list-customers
# Expected: table with 2 rows (Helena + Clinica)

# JSON format
$CE compadre-keys list-customers --format json
# Expected: JSON array with 2 objects, each has id/name/email/tax_id_kind/key_count/...

# With tombstones (none yet)
$CE compadre-keys list-customers --deleted
# Expected: same as active (no tombstones)
```

### 2.4 — `show <target>`

```bash
# Show customer by ID
$CE compadre-keys show "$C1"
# Expected: panel with name, email, tax_id_kind, notes, keys (0 so far)

# Show customer with audit tail
$CE compadre-keys show "$C1" --audit-tail 5
# Expected: panel + audit tail with "create_customer" event

# Show customer as JSON
$CE compadre-keys show "$C1" --format json
# Expected: {"type":"customer","target":{...}}

# Show non-existent ID
$CE compadre-keys show cust_00000000000000000000000000
echo "Exit: $?"
# Expected: exit 3, "Not found"
```

### 2.5 — `update-customer`: append-note (additive)

```bash
$CE compadre-keys update-customer "$C1" --add-note "paga em dia - lembrar dia 3"
# Expected: exit 0, "Updated: ... — note added"

# Verify notes contain dated entry
$CE sqlite3 /var/lib/compadre/registry.db \
  "SELECT notes FROM customer WHERE id='$C1';"
# Expected: contains "[YYYY-MM-DD] paga em dia - lembrar dia 3" (with date prefix)
```

### 2.6 — `update-customer`: replace-notes + name + email

```bash
$CE compadre-keys update-customer "$C1" \
  --name "Helena Ribeiro Silva" \
  --email "helena.silva@nova-clinica.com" \
  --notes "rebuilt history\n[2026-04-25] consolidated"
# Expected: exit 0, "Updated: ... — name changed, email changed, notes replaced"
```

**State verification (SQLite):**

```sql
SELECT name, email, notes FROM customer WHERE id='<C1>';
-- Expected: new name, new email, notes = exactly the --notes value
-- (no date-stamped prefix — replace mode overwrites)
```

### 2.7 — `update-customer`: edge cases

```bash
# No flags at all
$CE compadre-keys update-customer "$C1"
echo "Exit: $?"
# Expected: exit 2, "at least one of --name, --email, --add-note, or --notes is required"

# --add-note and --notes both provided (mutex)
$CE compadre-keys update-customer "$C1" --add-note "x" --notes "y"
echo "Exit: $?"
# Expected: exit 2, "mutually exclusive"

# --add-note empty/whitespace
$CE compadre-keys update-customer "$C1" --add-note "   "
echo "Exit: $?"
# Expected: exit 2

# --add-note with NUL
$CE compadre-keys update-customer "$C1" --add-note $'bad\x00char'
echo "Exit: $?"
# Expected: exit 2

# --add-note with newline (single-line enforced)
$CE compadre-keys update-customer "$C1" --add-note $'line1\nline2'
echo "Exit: $?"
# Expected: exit 2

# --notes empty/whitespace (silent wipe prevention)
$CE compadre-keys update-customer "$C1" --notes "   "
echo "Exit: $?"
# Expected: exit 2, "must not be empty or whitespace-only"

# Non-existent customer
$CE compadre-keys update-customer cust_00000000000000000000000000 --name "X"
echo "Exit: $?"
# Expected: exit 3, "Customer not found"

# Same-value write (no-op)
$CE compadre-keys update-customer "$C1" --name "Helena Ribeiro Silva"
# Expected: exit 0, "No changes: ... — already up to date"
# Verify: no new audit row for the no-op
```

---

## Task 3 — Key Verbs: issue-key, list-keys, update-key, rotate-key, revoke-key

**Goal:** Test the complete key lifecycle from issuance through revocation,
with all billing modes, RPM bounds, notes validation, and the rotation +
revocation edge cases.

### 3.1 — `issue-key`: happy paths

```bash
# Issue a manual billing key for Helena
$CE compadre-keys issue-key \
  --customer "$C1" \
  --rpm 60 \
  --billing-mode manual \
  --price 149 \
  --notes "WhatsApp bot"
# Expected: exit 0, one-shot panel with plaintext (cmpd_live_<43>)
# SAVE THE PLAINTEXT — it appears only once
KEY1_PLAINTEXT="cmpd_live_..."  # copy from output
KEY1_PREFIX="${KEY1_PLAINTEXT:10:8}"  # first 8 chars after cmpd_live_

# Issue a complimentary key for the company
$CE compadre-keys issue-key \
  --customer "$C2" \
  --rpm 100 \
  --billing-mode complimentary \
  --notes "Trial partner"
KEY2_PLAINTEXT="cmpd_live_..."
KEY2_PREFIX="${KEY2_PLAINTEXT:10:8}"
```

### 3.2 — `issue-key`: edge cases

```bash
# Missing --customer
$CE compadre-keys issue-key --rpm 60
echo "Exit: $?"
# Expected: exit 2

# Non-existent customer
$CE compadre-keys issue-key --customer cust_nonexistent --rpm 60
echo "Exit: $?"
# Expected: exit 3

# RPM out of range (0 or 10001)
$CE compadre-keys issue-key --customer "$C1" --rpm 0
echo "Exit: $?"
# Expected: exit 2

$CE compadre-keys issue-key --customer "$C1" --rpm 10001
echo "Exit: $?"
# Expected: exit 2

# Manual without --price
$CE compadre-keys issue-key --customer "$C1" --rpm 60 --billing-mode manual
echo "Exit: $?"
# Expected: exit 2, "--price is required"

# Complimentary with --price
$CE compadre-keys issue-key --customer "$C1" --rpm 60 --billing-mode complimentary --price 149
echo "Exit: $?"
# Expected: exit 2

# Negative price
$CE compadre-keys issue-key --customer "$C1" --rpm 60 --price -10
echo "Exit: $?"
# Expected: exit 2

# Invalid billing-mode
$CE compadre-keys issue-key --customer "$C1" --rpm 60 --billing-mode automated
echo "Exit: $?"
# Expected: exit 2
```

**State verification (SQLite) after 3.1–3.2:**

```sql
-- Verify both keys inserted
SELECT id, customer_id, key_prefix, status, billing_mode, rate_limit_rpm, price_monthly, paid_until
FROM api_key;
-- Expected: 2 rows (one manual active with paid_until=NULL, one complimentary active)

-- Verify manual key has NULL paid_until (trial window)
SELECT paid_until FROM api_key WHERE billing_mode='manual';
-- Expected: NULL (or empty)

-- Verify complimentary has price_monthly=0.0
SELECT price_monthly FROM api_key WHERE billing_mode='complimentary';
-- Expected: 0.0

-- Verify issue_key audit row (note: first_charge_date is NOT persisted in audit_log;
-- it is computed ephemerally in the CLI and shown once during issuance only)
SELECT details_json FROM audit_log WHERE action='issue_key' AND target_type='api_key';
-- Should contain {"billing_mode":"manual","rate_limit_rpm":60} — NO first_charge_date field
```

### 3.3 — `list-keys`

```bash
# Default (all)
$CE compadre-keys list-keys
# Expected: 2 rows in table

# By status
$CE compadre-keys list-keys --status active
$CE compadre-keys list-keys --status suspended  # should be empty

# By billing mode
$CE compadre-keys list-keys --billing-mode manual
$CE compadre-keys list-keys --billing-mode complimentary

# By customer
$CE compadre-keys list-keys --customer "$C1"
# Expected: 1 row (Helena's key)

# JSON format
$CE compadre-keys list-keys --format json
# Expected: JSON array with id, customer_id, key_prefix, status, etc.

# --paid-until-lte today
$CE compadre-keys list-keys --paid-until-lte today
# Expected: empty or trial keys (NULL paid_until behavior — check)
```

### 3.4 — `show <key-prefix>`

```bash
$CE compadre-keys show "$KEY1_PREFIX"
# Expected: detail panel: customer name, key prefix, status, billing, RPM, price, paid_until

$CE compadre-keys show "$KEY1_PREFIX" --audit-tail 10
# Expected: panel + audit tail showing issue_key event

$CE compadre-keys show "$KEY1_PREFIX" --format json
# Expected: {"type":"api_key","target":{...}}

# Show by full plaintext (operator convenience)
$CE compadre-keys show "$KEY1_PLAINTEXT"
# Expected: same as above (the resolver strips the cmpd_live_ prefix)
```

### 3.5 — `update-key`

```bash
# Update RPM
$CE compadre-keys update-key "$KEY1_PREFIX" --rpm 120
# Expected: exit 0, "Updated: ... — rpm: 60→120"

# Update price
$CE compadre-keys update-key "$KEY1_PREFIX" --price 199
# Expected: "price: R$ 149,00→R$ 199,00"

# Update notes
$CE compadre-keys update-key "$KEY1_PREFIX" --notes "upgraded RPM and price"
# Expected: "notes updated"

# All at once
$CE compadre-keys update-key "$KEY1_PREFIX" --rpm 60 --price 149 --notes "rolled back"
# Expected: all three clauses in confirmation

# No changes provided
$CE compadre-keys update-key "$KEY1_PREFIX"
echo "Exit: $?"
# Expected: exit 2

# Non-existent key
$CE compadre-keys update-key "00000000" --rpm 60
echo "Exit: $?"
# Expected: exit 3

# Complimentary + --price
$CE compadre-keys update-key "$KEY2_PREFIX" --price 99
echo "Exit: $?"
# Expected: exit 2, "not applicable to complimentary"
```

**State verification (SQLite):**

```sql
SELECT key_prefix, rate_limit_rpm, price_monthly, notes
FROM api_key WHERE key_prefix='<KEY1_PREFIX>';
-- Expected: rpm=60, price=149.0, notes="rolled back"
```

### 3.6 — `rotate-key`

```bash
# Rotate the key
$CEI compadre-keys rotate-key "$KEY1_PREFIX"
# Prompts: "This will rotate key ... — the old plaintext stops working immediately. Continue? [y/N]"
# Type: y
# Expected: exit 0, new KeyPanel with new plaintext
# SAVE the new plaintext
KEY1_NEW_PLAINTEXT="cmpd_live_..."
KEY1_NEW_PREFIX="${KEY1_NEW_PLAINTEXT:10:8}"
```

**Verification:**

```bash
# Old plaintext → 401
curl -k -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $KEY1_PLAINTEXT" \
  https://10.10.10.208/health
# Expected: 401

# New plaintext → 200
curl -k -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $KEY1_NEW_PLAINTEXT" \
  https://10.10.10.208/health
# Expected: 200
```

**State verification (SQLite):**

```sql
-- key_id is preserved, key_hash and key_prefix changed
SELECT id, key_prefix FROM api_key WHERE id='<original key_id>';
-- Expected: same id, different prefix

-- Verify audit row written
SELECT action, details_json FROM audit_log WHERE action='rotate_key' ORDER BY id DESC LIMIT 1;
-- Expected: rotate_key with old_prefix / new_prefix in details
```

### 3.7 — `rotate-key`: edge cases

```bash
# Rotate a revoked key → should be rejected
# (we'll test this after revoking a key below)

# Rotate non-existent prefix
$CE compadre-keys rotate-key "00000000"
echo "Exit: $?"
# Expected: exit 3

# Rotate with --yes (scripting)
$CE compadre-keys rotate-key "$KEY1_NEW_PREFIX" --yes
# Expected: no prompt, exit 0, new plaintext displayed
```

### 3.8 — `revoke-key`

```bash
# Revoke the complimentary key (won't be used for billing tests)
$CEI compadre-keys revoke-key "$KEY2_PREFIX" \
  --reason "bundle contract ended 2026-09-30"
# Prompts for confirmation → y
# Expected: exit 0, "Revoked: ... — reason: bundle contract ended 2026-09-30"
```

**Verification:**

```bash
# Revoked key → 401
curl -k -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $KEY2_PLAINTEXT" \
  https://10.10.10.208/health
# Expected: 401
```

### 3.9 — `revoke-key`: edge cases

```bash
# Revoke an already-revoked key
$CE compadre-keys revoke-key "$KEY2_PREFIX" --reason "double tap"
echo "Exit: $?"
# Expected: exit 3, "Key already revoked"

# Revoke with missing --reason
$CE compadre-keys revoke-key "$KEY1_NEW_PREFIX"
echo "Exit: $?"
# Expected: exit 2 (--reason is required)

# Empty reason
$CE compadre-keys revoke-key "$KEY1_NEW_PREFIX" --reason "   "
echo "Exit: $?"
# Expected: exit 2

# NUL in reason
$CE compadre-keys revoke-key "$KEY1_NEW_PREFIX" --reason $'bad\x00char'
echo "Exit: $?"
# Expected: exit 2

# Newline in reason
$CE compadre-keys revoke-key "$KEY1_NEW_PREFIX" --reason $'line1\nline2'
echo "Exit: $?"
# Expected: exit 2

# Non-existent key
$CE compadre-keys revoke-key "00000000" --reason "test"
echo "Exit: $?"
# Expected: exit 3
```

**State verification (SQLite) after 3.6–3.9:**

```sql
SELECT key_prefix, status FROM api_key;
-- Expected: KEY1 active (or twice-rotated), KEY2 revoked

-- Verify revoked key audit row
SELECT action, details_json FROM audit_log WHERE action='revoke_key' ORDER BY id DESC LIMIT 1;
-- Expected: contains reason in details_json
```

---

## Task 4 — Billing Verbs: mark-paid, suspend, reactivate, at-risk, revenue

**Goal:** Test the complete billing lifecycle: mark a key paid (both
`--until` and `--months` modes), verify the anchoring logic, test manual
suspend/reactivate, at-risk preview, and revenue reporting (including
`--as-of` audit replay).

### 4.1 — `mark-paid --until`: exact-date

```bash
# Set an exact paid_until date
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --until 2026-12-31
# Expected: exit 0, "Marked paid: ... → paid until 2026-12-31"
```

**State verification (SQLite):**

```sql
SELECT key_prefix, status, paid_until FROM api_key WHERE key_prefix='<KEY1_NEW_PREFIX>';
-- Expected: status=active, paid_until='2026-12-31'
```

### 4.2 — `mark-paid --months`: extend from current paid_until

```bash
# Key is currently paid until 2026-12-31 (future relative to today)
# Extend by 3 months → should be 2027-03-31
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --months 3
# Expected: "Marked paid: ... → paid until 2027-03-31 (extended 3 months)"
```

**State verification (SQLite):**

```sql
SELECT paid_until FROM api_key WHERE key_prefix='<KEY1_NEW_PREFIX>';
-- Expected: '2027-03-31'

-- Verify audit row carries mode=months
SELECT details_json FROM audit_log WHERE action='mark_paid' ORDER BY id DESC LIMIT 1;
-- Expected: {"mode":"months","months":3}
```

### 4.3 — `mark-paid --months`: anchor from today when expired

First, expire the key by setting `paid_until` in the past:

```bash
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --until 2026-01-01
# Now paid_until is in the past
```

Then test `--months` anchoring:

```bash
# Current date is somewhere in 2026-04. Key expired on 2026-01-01.
# --months 1 should extend by 1 month from TODAY (not from 2026-01-01)
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --months 1
# Expected: paid_until = today + 1 month (roughly 2026-05-26)
```

**State verification (SQLite):**

```sql
-- Verify paid_until is approx today + 1 month, NOT 2026-02-01
SELECT paid_until FROM api_key WHERE key_prefix='<KEY1_NEW_PREFIX>';
-- Should be 2026-05-XX (month-end clamping may apply)
```

### 4.4 — `mark-paid --price-monthly` (atomic with paid_until)

```bash
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --months 2 --price-monthly 199.90
# Expected: "Marked paid: ... → paid until ... (extended 2 months, price R$ 199.90/mo)"
```

**State verification (SQLite):**

```sql
SELECT price_monthly, paid_until FROM api_key WHERE key_prefix='<KEY1_NEW_PREFIX>';
-- Expected: price_monthly=199.9, paid_until updated in same transaction

-- Verify audit row carries price change
SELECT details_json FROM audit_log WHERE action='mark_paid' ORDER BY id DESC LIMIT 1;
-- Expected: {"mode":"months","months":2,"price_monthly_changed":true,"old":149.0,"new":199.9}
```

### 4.5 — `mark-paid`: edge cases

```bash
# Neither --until nor --months
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX"
echo "Exit: $?"
# Expected: exit 2

# Both --until and --months (mutex)
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --until 2026-12-31 --months 1
echo "Exit: $?"
# Expected: exit 2

# --months out of range
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --months 0
echo "Exit: $?"
# Expected: exit 2

$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --months 37
echo "Exit: $?"
# Expected: exit 2

# --price-monthly NaN or negative
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --months 1 --price-monthly -10
echo "Exit: $?"
# Expected: exit 2

# mark-paid a complimentary key
$CE compadre-keys mark-paid "$KEY2_PREFIX" --months 1
# Note: KEY2_PREFIX was revoked. But the command would show "complimentary" for active ones
# Expected for a complimentary: informational message, no mutation

# mark-paid a revoked key
$CE compadre-keys mark-paid "$KEY2_PREFIX" --months 1
echo "Exit: $?"
# Expected: exit 3, "Key is revoked"

# Non-existent key
$CE compadre-keys mark-paid "00000000" --months 1
echo "Exit: $?"
# Expected: exit 3

# --until with non-ISO date
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --until "tomorrow"
echo "Exit: $?"
# Expected: exit 2
```

### 4.6 — `suspend` and `reactivate`

```bash
# Suspend a key
$CE compadre-keys suspend "$KEY1_NEW_PREFIX" --reason "Payment overdue - 14 days"
# Expected: exit 0, "Suspended: ..."

# Verify: key → 401
curl -k -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $KEY1_NEW_PLAINTEXT" \
  https://10.10.10.208/health
# Expected: 401

# Reactivate (without extending billing — paid_until unchanged)
$CE compadre-keys reactivate "$KEY1_NEW_PREFIX"
# Expected: exit 0, "Reactivated: ..."

# Verify: key → 200 again
curl -k -s -o /dev/null -w "%{http_code}" \
  -H "Authorization: Bearer $KEY1_NEW_PLAINTEXT" \
  https://10.10.10.208/health
# Expected: 200
```

**State verification (SQLite):**

```sql
-- Verify status transitions
SELECT key_prefix, status, paid_until FROM api_key WHERE key_prefix='<KEY1_NEW_PREFIX>';
-- Expected: status='active' (reactivated), paid_until unchanged

-- Verify audit rows
SELECT action FROM audit_log WHERE target_id='<KEY1_ID>' ORDER BY id DESC LIMIT 3;
-- Expected: reactivate_key, suspend_key, mark_paid (descending)
```

### 4.7 — `suspend` / `reactivate`: edge cases

```bash
# Suspend an already-suspended key
$CE compadre-keys suspend "$KEY1_NEW_PREFIX"
echo "Exit: $?"
# Expected: exit 3, "already suspended" (status is active though — adapt)

# Let's first suspend again, then test
$CE compadre-keys suspend "$KEY1_NEW_PREFIX" --reason "Testing double-suspend guard"
$CE compadre-keys suspend "$KEY1_NEW_PREFIX" --reason "double"
echo "Exit: $?"
# Expected: exit 3, "already suspended"

# Reactivate an already-active
$CE compadre-keys reactivate "$KEY1_NEW_PREFIX"  # first reactivation
$CE compadre-keys reactivate "$KEY1_NEW_PREFIX"  # second attempt
echo "Exit: $?"
# Expected: exit 3, "already active"

# Suspend a revoked key
$CE compadre-keys suspend "$KEY2_PREFIX"
echo "Exit: $?"
# Expected: exit 3

# Reactivate a revoked key
$CE compadre-keys reactivate "$KEY2_PREFIX"
echo "Exit: $?"
# Expected: exit 3
```

### 4.8 — `at-risk`: preview expiring keys

```bash
# Set a key to expire in 3 days
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --until "$(date -d '+3 days' +%Y-%m-%d)"
# (date command runs on the VPS host — adjust for macOS if testing from Mac)

# 5-day window should include it
$CE compadre-keys at-risk --window 5d
# Expected: table showing KEY1 with days_left=3

# 1-day window should NOT include it
$CE compadre-keys at-risk --window 1d
# Expected: "No keys at risk in the next 1 days."

# JSON format
$CE compadre-keys at-risk --window 5d --format json
# Expected: JSON array with days_left field
```

### 4.9 — `revenue`: current month

```bash
# Current month revenue report
$CE compadre-keys revenue --month current
# Expected: panel with paid/pending/suspended/complimentary counts, MRR, projected

# Specific month
$CE compadre-keys revenue --month 2026-05

# JSON format
$CE compadre-keys revenue --month current --format json
# Expected: JSON with month, mrr, projected, etc.
```

### 4.10 — `revenue --as-of`: audit replay

```bash
# Replay state as of a past date
$CE compadre-keys revenue --month 2026-04 --as-of 2026-04-15
# Expected: historical snapshot reconstructed from audit log

# --as-of in the future
$CE compadre-keys revenue --month current --as-of 2027-01-01
echo "Exit: $?"
# Expected: exit 2, "cannot be in the future"

# --as-of before earliest audit row
$CE compadre-keys revenue --month current --as-of 2020-01-01
echo "Exit: $?"
# Expected: exit 2, "predates the audit log"
```

---

## Task 5 — Data-Subject Verbs (LGPD): export-customer, correct-tax-id, delete-customer

**Goal:** Test the three LGPD-focused verbs: export (Art. 18 II/V),
tax-ID correction (Art. 18 III), and hard-delete (Art. 18 VI). Verify all
contracts: tombstone shape, FK preservation vs. delete+recreate, opt-in
audit export, truncation signalling.

### 5.1 — `export-customer`: default (no audit)

```bash
$CE compadre-keys export-customer "$C1" --format json
# Expected: JSON with id, name, email, tax_id_kind, tax_id (decrypted),
#           registered_at, notes, keys[].
#           audit_events MUST NOT be present.
```

**Verification:** Pipe to `jq` and assert:

```bash
$CE compadre-keys export-customer "$C1" --format json | jq 'keys'
# Expected: ["email","id","keys","name","notes","registered_at","tax_id","tax_id_kind"]
# "audit_events" must NOT be in this list.

# Verify tax_id is decrypted (CPF format: xxx.xxx.xxx-xx)
$CE compadre-keys export-customer "$C1" --format json | jq '.tax_id'
# Expected: "111.444.777-35"
```

### 5.2 — `export-customer --include-audit`: opt-in

```bash
$CE compadre-keys export-customer "$C1" --format json --include-audit | jq 'keys'
# Expected: includes "audit_events", "audit_events_total", "audit_events_truncated"

# Verify chronological order (oldest first)
$CE compadre-keys export-customer "$C1" --format json --include-audit | jq '.audit_events[0].occurred_at,.audit_events[-1].occurred_at'
# Expected: first < last

# Verify no PII in audit details
$CE compadre-keys export-customer "$C1" --format json --include-audit | jq '.audit_events[].details | select(. != null) | keys'
# Should NOT contain: cpf, cnpj, tax_id, name, email, key_plaintext, authorization
```

### 5.3 — `export-customer`: edge cases

```bash
# YAML format
$CE compadre-keys export-customer "$C1" --format yaml
# Expected: valid YAML

# Invalid customer ID shape
$CE compadre-keys export-customer "bad-id"
echo "Exit: $?"
# Expected: exit 2, "invalid customer id"

# Non-existent customer
$CE compadre-keys export-customer cust_00000000000000000000000000
echo "Exit: $?"
# Expected: exit 3, "Not found"

# Invalid format
$CE compadre-keys export-customer "$C1" --format xml
echo "Exit: $?"
# Expected: exit 2
```

### 5.4 — `correct-tax-id`: preserve customer_id + FKs

```bash
# Before correction, capture FK state
$CE sqlite3 /var/lib/compadre/registry.db \
  "SELECT id, customer_id FROM api_key WHERE customer_id='$C1';"
# Save the key IDs

# Correct the CPF
$CEI compadre-keys correct-tax-id "$C1" \
  --new-cpf "222.333.444-55" \
  --reason "operator-typo correction"
# Prompts → y
# Expected: exit 0, "Tax-ID corrected: ... — kind cpf"
```

**State verification (SQLite):**

```sql
-- customer_id is unchanged
SELECT id FROM customer WHERE id='<C1>';
-- Expected: same ID

-- All api_key FKs still point to same customer
SELECT customer_id FROM api_key WHERE customer_id='<C1>';
-- Expected: all keys from before still reference C1

-- tax_id_encrypted changed (different ciphertext)
-- We can't compare ciphertexts directly but can verify it's non-empty
SELECT length(tax_id_encrypted) FROM customer WHERE id='<C1>';
-- Expected: > 0

-- tax_id_lookup changed (different HMAC)
SELECT length(tax_id_lookup) FROM customer WHERE id='<C1>';
-- Expected: 32 (valid BLOB)

-- Audit row written
SELECT action, details_json FROM audit_log WHERE action='correct_tax_id' ORDER BY id DESC LIMIT 1;
-- Expected: {"old_kind":"cpf","new_kind":"cpf","reason":"operator-typo correction"}
-- MUST NOT contain the old or new CPF value
```

**Verify via export:**

```bash
$CE compadre-keys export-customer "$C1" --format json | jq '.tax_id'
# Expected: "222.333.444-55" (new CPF)
```

### 5.5 — `correct-tax-id`: edge cases

```bash
# Correct to a tax-ID already in use
$CE compadre-keys correct-tax-id "$C1" --new-cnpj "11222333000181" --reason "test dup" --yes
# 11.222.333/0001-81 is the company's CNPJ
echo "Exit: $?"
# Expected: exit 2 (DuplicateTaxIdError)

# Invalid new CPF
$CE compadre-keys correct-tax-id "$C1" --new-cpf "00000000000" --reason "test" --yes
echo "Exit: $?"
# Expected: exit 2

# Missing --reason
$CE compadre-keys correct-tax-id "$C1" --new-cpf "11144477735" --reason ""
echo "Exit: $?"
# Expected: exit 2

# Non-existent customer
$CE compadre-keys correct-tax-id cust_00000000000000000000000000 --new-cpf "11144477735" --reason "test"
echo "Exit: $?"
# Expected: exit 3
```

### 5.6 — `delete-customer --hard`: tombstone contract

```bash
# Delete Helena's customer record
$CEI compadre-keys delete-customer "$C1" --hard
# This should fail: --hard without --yes requires interactive confirm
# If prompted, answer y
# Expected: exit 0, "Deleted: ... — N keys purged"
```

**Tombstone verification (SQLite):** This is the most critical state check.

```sql
-- Canonical tombstone signal
SELECT id, deleted_at FROM customer WHERE id='<C1>';
-- Expected: deleted_at IS NOT NULL

-- PII scrubbed
SELECT name, email, notes FROM customer WHERE id='<C1>';
-- Expected: name='', email='', notes=''

-- tax_id_encrypted is zero-length BLOB (NOT NULL!)
SELECT length(tax_id_encrypted), typeof(tax_id_encrypted)
FROM customer WHERE id='<C1>';
-- Expected: length=0, typeof=blob
-- This is X'' (empty blob), NOT NULL

-- tax_id_lookup is NULL (cleared to remove from unique index)
SELECT tax_id_lookup FROM customer WHERE id='<C1>';
-- Expected: NULL (or empty)

-- All api_keys DELETEd
SELECT COUNT(*) FROM api_key WHERE customer_id='<C1>';
-- Expected: 0

-- Audit row written
SELECT action FROM audit_log WHERE action='delete_customer' AND target_id='<C1>';
-- Expected: 1 row

-- Tombstone visible in list-customers --deleted
$CE compadre-keys list-customers --deleted
# Expected: table with the tombstone row
```

**Re-register with same CPF (tombstone exception):**

```bash
# The tombstone clears tax_id_lookup, so re-registration should succeed
$CE compadre-keys create-customer \
  --name "Helena Re-registered" \
  --email "helena-new@test.com" \
  --cpf "22233344455"
# Expected: exit 0, NEW customer ID (different from original C1)
```

**State verification:**

```sql
-- Old customer still tombstoned
SELECT id, name, deleted_at FROM customer WHERE id='<C1>';
-- Expected: same tombstone state

-- New customer is a different row
SELECT id, name, email FROM customer WHERE name='Helena Re-registered';
-- Expected: different cust_<ulid>, clean record

-- Two rows exist: one tombstone + one active
SELECT COUNT(*) FROM customer;
-- Expected: 3 (Clinica active, Helena tombstone, Helena re-registered)
```

### 5.7 — `delete-customer`: edge cases

```bash
# --hard not provided
$CE compadre-keys delete-customer cust_00000000000000000000000000
echo "Exit: $?"
# Expected: exit 2, "--hard is required"

# Non-existent customer
$CE compadre-keys delete-customer cust_00000000000000000000000000 --hard --yes
echo "Exit: $?"
# Expected: exit 3

# Invalid ID shape
$CE compadre-keys delete-customer "bad" --hard
echo "Exit: $?"
# Expected: exit 2
```

---

## Task 6 — System Verbs: suspend-sweep, health, cache-status, rotate-encryption-key

**Goal:** Test the four system-level verbs covering automated operations,
observability, and encryption key rotation.

### 6.1 — `suspend-sweep --dry-run`

```bash
# Set up: expire a key (paid_until in the past) while keeping it active
$CE compadre-keys mark-paid "$KEY1_NEW_PREFIX" --until 2026-01-01
$CE compadre-keys reactivate "$KEY1_NEW_PREFIX"  # ensure active status
# Now KEY1 is active but paid_until is in the past

# Dry run
$CE compadre-keys suspend-sweep --dry-run
# Expected: table showing KEY1 would be suspended
# Verify: key is still active (dry run didn't mutate)
```

**Verify no mutation:**

```sql
SELECT key_prefix, status FROM api_key WHERE key_prefix='<KEY1_NEW_PREFIX>';
-- Expected: status='active' (dry run didn't change it)
```

### 6.2 — `suspend-sweep`: execute

```bash
$CE compadre-keys suspend-sweep
# Expected: exit 0, "Suspension sweep completed: 1 key(s) suspended at ... (cutoff YYYY-MM-DD)."
```

**State verification (SQLite):**

```sql
SELECT key_prefix, status FROM api_key WHERE key_prefix='<KEY1_NEW_PREFIX>';
-- Expected: status='suspended'

-- Sweep only targets manual billing keys
-- Complimentary keys with expired paid_until should NOT be suspended
-- (no complimentary keys are currently active, but verify the filter works)

-- Audit rows written
SELECT action, actor FROM audit_log WHERE action='suspend_key' AND actor='cron:suspension-sweep';
-- Expected: ≥1 row with cron actor

-- Sweep cutoff = today in billing TZ (strict paid_until < today)
-- paid_until == today: NOT suspended (strict inequality)
```

### 6.3 — `health`

```bash
# Table mode (always exits 0)
$CE compadre-keys health
echo "Exit: $?"
# Expected: 0, even with warnings (yellow text)

# JSON mode — healthy deployment should exit 0
$CE compadre-keys health --format json | jq '.is_healthy'
echo "Exit: $?"
# Expected: true, exit 0

# JSON mode — check structure
$CE compadre-keys health --format json | jq 'keys'
# Expected: ["is_healthy","checked_at","checks"]

$CE compadre-keys health --format json | jq '.checks | length'
# Expected: 10 (DB, Schema, Customers, Keys, Sweep, Cron, Backup Cron, TZ Drift, Backup, Auth Cache)

# Each check has: label, severity, value, detail, data
$CE compadre-keys health --format json | jq '.checks[0] | keys'
# Expected: ["data","detail","label","severity","value"] (detail and data are always present, may be null)
```

### 6.4 — `health --format json` exit 7 trigger

To verify exit 7, we need to create a warning condition. The easiest is
to wait for the backup check to warn (no backup taken yet) or check for
cron markers (they exist from bootstrap).

```bash
# On a fresh deploy with no backups, the "Backup" row should be warn
$CE compadre-keys health --format json | jq '.checks[] | select(.severity != "ok" and .severity != "info")'
# Look for warn/fail rows

# If there's a warn row, JSON exit should be 7
$CE compadre-keys health --format json
echo "Exit: $?"
# Expected: 7 IF any check is warn or fail; 0 otherwise
```

### 6.5 — `cache-status`

```bash
# Table mode
$CE compadre-keys cache-status
# Expected: panel showing TTL, propagation bound, and live stats (if API is running)

$CE compadre-keys cache-status --format json | jq '.'
# Expected: JSON with ttl_seconds, propagation_bound_message, snapshot_available, etc.
```

### 6.6 — `rotate-encryption-key`

**Step 1:** Generate a new Fernet key:

```bash
NEW_KEY="$($CE python -c 'from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())')"
echo "New key: $NEW_KEY"
# Save this for .env update later
```

**Step 2:** Count active customers before rotation:

```bash
ACTIVE_BEFORE=$($CE sqlite3 /var/lib/compadre/registry.db \
  "SELECT COUNT(*) FROM customer WHERE deleted_at IS NULL;")
echo "Active customers before: $ACTIVE_BEFORE"
```

**Step 3:** Run the rotation:

```bash
$CEI compadre-keys rotate-encryption-key --new-key "$NEW_KEY"
# Prompt → y
# Expected: exit 0, "Encryption key rotated. Update KEY_REGISTRY_ENCRYPTION_KEY in .env to: <new-key>"
# SAVE the output — it prints the new key as a copy-paste hint
```

**Step 4:** Verify the audit row:

```sql
SELECT action, details_json FROM audit_log WHERE action='rotate_encryption_key' ORDER BY id DESC LIMIT 1;
-- Expected: {"rows_re_encrypted": N} where N = active customer count
-- Tombstoned customers counted separately (or simply excluded)
```

**Step 5:** Update `.env` and restart the service:

```bash
# On the VPS:
cd ~/compadre
# Backup the old key
grep KEY_REGISTRY_ENCRYPTION_KEY .env >> ~/old-encryption-key.txt
# Update .env with the new key
sed -i "s/^KEY_REGISTRY_ENCRYPTION_KEY=.*/KEY_REGISTRY_ENCRYPTION_KEY=$NEW_KEY/" .env
# Restart
docker compose restart compadre-stt
# Wait for healthy
sleep 30
curl -k https://10.10.10.208/health
```

**Step 6:** Verify decryption after restart:

```bash
# export-customer must still decrypt tax IDs
$CE compadre-keys export-customer "$C3" --format json | jq '.tax_id'
# Expected: decrypted CPF/CNPJ (NOT an error about InvalidToken)
# If this fails with InvalidToken → the .env key doesn't match
```

**Step 7:** Verify tombstone not re-encrypted:

```bash
# Count re-encrypted rows should equal active customers (not total)
ACTIVE_AFTER=$($CE sqlite3 /var/lib/compadre/registry.db \
  "SELECT COUNT(*) FROM customer WHERE deleted_at IS NULL;")
echo "Active after: $ACTIVE_AFTER"
# Should match rows_re_encrypted in the audit row
# Total rows > active (tombstones were skipped)
```

### 6.7 — `rotate-encryption-key`: edge cases

```bash
# Invalid Fernet key shape
$CE compadre-keys rotate-encryption-key --new-key "not-a-valid-fernet-key" --yes
echo "Exit: $?"
# Expected: exit 4, "not a valid Fernet key"

# Empty key
$CE compadre-keys rotate-encryption-key --new-key "" --yes
echo "Exit: $?"
# Expected: exit 4

# Missing --new-key
$CE compadre-keys rotate-encryption-key
echo "Exit: $?"
# Expected: exit 2

# With wrong current key (manually corrupt .env, restart, then try rotate)
# This tests the InvalidToken rollback path
```

---

## Task 7 — Exit Code Exhaustive Verification

**Goal:** Systematically trigger every exit code with at least one real
condition and record the trigger.

### 7.1 — Exit code table

| Exit | Trigger | Command |
|------|---------|---------|
| **0** | Any successful command | `compadre-keys list-customers` |
| **2** | Invalid CPF checksum | `create-customer --cpf "111.444.777-36" ...` |
| **2** | Duplicate tax-ID | `create-customer --cpf <existing-cpf> ...` |
| **2** | NUL in `--name` | `create-customer --name $'x\x00y' ...` |
| **2** | `--months` + `--until` together | `mark-paid ... --months 1 --until 2026-12-31` |
| **2** | Empty `--reason` | `revoke-key ... --reason "   "` |
| **2** | Oversize `--notes` | `update-customer ... --notes "$(python3 -c 'print("x"*8193)')"` |
| **2** | Invalid `--window` format | `at-risk --window 5w` |
| **2** | `--as-of` in the future | `revenue --month current --as-of 2099-01-01` |
| **2** | Invalid `--format` | `list-customers --format xml` |
| **2** | Confirm abort (non-TTY without `--yes`) | `revoke-key ... --reason "test"` (via `docker exec` without TTY) |
| **3** | Customer not found | `show cust_00000000000000000000000000` |
| **3** | Key not found | `update-key 00000000 --rpm 60` |
| **3** | Already revoked | `revoke-key <revoked-prefix> --reason "test"` |
| **3** | Already suspended | `suspend <suspended-prefix>` |
| **3** | Already active | `reactivate <active-prefix>` |
| **3** | Key ambiguous prefix | (issue two keys with same first 8 chars — unlikely) |
| **4** | Invalid Fernet key shape | `rotate-encryption-key --new-key "bad" --yes` |
| **5** | DB error (corrupt path) | `KEY_REGISTRY_DB_PATH=/nonexistent/dir/db.sqlite compadre-keys list-customers` |
| **5** | InvalidToken on export | (requires key mismatch — test after rotate-encryption-key with wrong .env) |
| **6** | Missing `KEY_REGISTRY_ENCRYPTION_KEY` | `KEY_REGISTRY_ENCRYPTION_KEY="" compadre-keys list-customers` |
| **6** | Invalid `BILLING_TIMEZONE` | `BILLING_TIMEZONE="Mars/Base" compadre-keys list-customers` |
| **6** | Unreadable `.env` | `chmod 000 .env; compadre-keys list-customers; chmod 644 .env` |
| **7** | `health --format json` with warn/fail | After removing backup file or cron marker |

### 7.2 — Exit code verification procedure

For each row in the table above, run the command and capture `$?`. Log
any discrepancy between expected and actual exit code as a bug.

```bash
# Helper function on the VPS:
test_exit() {
  local expected="$1"
  shift
  echo "--- Testing exit code $expected: $*"
  $CE "$@"
  local actual="$?"
  if [ "$actual" -eq "$expected" ]; then
    echo "PASS: exit $actual as expected"
  else
    echo "FAIL: expected exit $expected, got exit $actual"
  fi
  echo
}
```

---

## Task 8 — Invariant Verification

**Goal:** Verify every architecture-level invariant using `grep`/`rg` inside
the container.

### 8.1 — Single-Fernet-call invariant

```bash
$CE sh -c 'grep -rn "fernet\.decrypt" /app/src/compadre/ --include="*.py"'
# Expected: exactly 1 hit in crypto.py::decrypt_tax_id

$CE sh -c 'grep -rn "from cryptography.fernet import" /app/src/compadre/ --include="*.py"'
# Expected: crypto.py (encrypt/decrypt/rotate), possibly system.py (rotate-encryption-key --new-key Fernet() call)
# Verify no other module imports Fernet directly
```

### 8.2 — No raw HTTPException

```bash
$CE sh -c 'grep -rn "HTTPException" /app/src/compadre/ --include="*.py" | grep -v "CompadreError\|\.pyc\|test_"'
# Expected: 0 hits in src/ (only CompadreError subclasses)
# Note: main.py imports HTTPException for the 404/405 handlers but uses it through CompadreError
# Verify: all raise statements use CompadreError subclasses, never raw HTTPException
```

### 8.3 — No raw os.environ

```bash
$CE sh -c 'grep -rn "os\.environ\[" /app/src/compadre/ --include="*.py" | grep -v "test_\|\.pyc"'
# Expected: 0 hits (all config via pydantic-settings)
```

### 8.4 — No raw print()

```bash
$CE sh -c 'grep -rn "^[^#]*\bprint(" /app/src/compadre/ --include="*.py" | grep -v "test_\|\.pyc\|__init__"'
# Expected: 0 hits in core application code
# (Rich console.print() is allowed — the check is for builtin print())
```

### 8.5 — Backup naming invariant

```bash
# The glob pattern in health.py must match the backup playbook output
$CE sh -c 'grep -n "backup-" /app/src/compadre/keys/health.py'
# Expected: glob pattern *.backup-*.tar.gz.enc

# Verify backup archive naming on the VPS host:
ls /var/lib/compadre/*.backup-*.tar.gz.enc 2>/dev/null || echo "No backups yet (expected until backup playbook runs)"
```

### 8.6 — Audit forbidden keys

```bash
$CE sh -c 'grep -n "_FORBIDDEN_DETAIL_KEYS" /app/src/compadre/keys/audit.py'
# Expected: blocklist definition

# Verify every audit writer uses the filter
$CE sh -c 'grep -rn "filter_forbidden_keys\|_FORBIDDEN_DETAIL_KEYS" /app/src/compadre/ --include="*.py" | grep -v "\.pyc"'
# Expected: audit.py (write-side assertion + read-side filter), possibly CLI export path
```

### 8.7 — Import graph verification

```bash
# No upward imports: keys modules don't import from compadre top-level
$CE sh -c 'grep -rn "from compadre\.\(api\|stt\|preprocessing\|rate_limit\|formatters\|download\)" /app/src/compadre/keys/ --include="*.py"'
# Expected: 0 hits

# No circular imports
$CE python -c "import compadre; import compadre.keys; import compadre.keys.cli; print('OK')"
```

### 8.8 — LGPD tombstone invariant

Already verified in Task 5.6. Re-confirm:

```sql
-- Every tombstoned row must have:
--   tax_id_encrypted = X'' (length=0, typeof=blob, NOT NULL)
--   name = ''
--   email = ''
--   notes = ''
--   tax_id_lookup IS NULL
--   deleted_at IS NOT NULL
SELECT id,
       CASE WHEN length(tax_id_encrypted)=0 AND typeof(tax_id_encrypted)='blob'
            THEN 'OK' ELSE 'FAIL' END AS tax_id_check,
       CASE WHEN name='' THEN 'OK' ELSE 'FAIL' END AS name_check,
       CASE WHEN email='' THEN 'OK' ELSE 'FAIL' END AS email_check,
       CASE WHEN notes='' THEN 'OK' ELSE 'FAIL' END AS notes_check,
       CASE WHEN tax_id_lookup IS NULL THEN 'OK' ELSE 'FAIL' END AS lookup_check,
       CASE WHEN deleted_at IS NOT NULL THEN 'OK' ELSE 'FAIL' END AS deleted_check
FROM customer
WHERE deleted_at IS NOT NULL;
-- Every row should show all OK
```

### 8.9 — No PII in audit log

```sql
-- Check that details_json contains no forbidden keys
-- This is a spot-check: search for known patterns
SELECT id, details_json FROM audit_log
WHERE details_json LIKE '%cpf%'
   OR details_json LIKE '%cnpj%'
   OR details_json LIKE '%tax_id%'
   OR details_json LIKE '%name%'
   OR details_json LIKE '%email%'
   OR details_json LIKE '%key_plaintext%'
   OR details_json LIKE '%authorization%';
-- Expected: 0 rows (the filter should catch all at write time)
-- NOTE: detail keys like "customer_name", "key_prefix", "old_kind" are OK
-- The forbidden list is the blocklisted KEYS, not substrings
```

### 8.10 — Counter snapshot files

```bash
# Send a few requests to generate counter data
for i in $(seq 1 5); do
  curl -k -s -o /dev/null -H "Authorization: Bearer $KEY1_NEW_PLAINTEXT" \
    https://10.10.10.208/health
done

# Wait 15 seconds for a snapshot write
sleep 20

# Verify snapshot file exists
$CE ls -la /var/lib/compadre/registry.db.counters.*.json
# Expected: at least one file

# Verify structure
$CE cat /var/lib/compadre/registry.db.counters.*.json | python3 -m json.tool | head -30
# Expected: JSON with per-key entries
```

---

## Task 9 — Concurrent Access and Race Condition Smoke Tests

**Goal:** Verify no DB corruption when the CLI operates while the API
serves traffic.

### 9.1 — issue-key while API is serving

```bash
# In one terminal: loop of health requests
for i in $(seq 1 50); do
  curl -k -s -o /dev/null -H "Authorization: Bearer $KEY1_NEW_PLAINTEXT" \
    https://10.10.10.208/health &
done

# In another: issue a key
$CE compadre-keys issue-key --customer "$C3" --rpm 60 --billing-mode manual --price 99

# Wait for curls to finish
wait

# Verify no DB errors, no corruption
$CE compadre-keys health
```

### 9.2 — rotate-key while serving traffic

```bash
# Start health requests
for i in $(seq 1 20); do
  curl -k -s -o /dev/null -H "Authorization: Bearer $KEY1_NEW_PLAINTEXT" \
    https://10.10.10.208/health &
done

# Rotate the key mid-traffic
$CE compadre-keys rotate-key "$KEY1_NEW_PREFIX" --yes

# Some requests may get 401 (cache miss after rotation), others 200 (cache hit)
# All should complete without errors
wait

# Verify registry is consistent
$CE sqlite3 /var/lib/compadre/registry.db "SELECT COUNT(*) FROM api_key;"
$CE compadre-keys health
```

---

## Task 10 — Cleanup: Reset to Known State

After all tests, clean up the test data:

```bash
# List all customers
$CE compadre-keys list-customers --deleted --format json | jq -r '.[].id'

# For each test customer, hard-delete (this will also delete all keys)
for CID in $($CE compadre-keys list-customers --format json | jq -r '.[].id'); do
  echo "Deleting $CID..."
  $CE compadre-keys delete-customer "$CID" --hard --yes
done

# Verify clean state
$CE sqlite3 /var/lib/compadre/registry.db \
  "SELECT 'customers', COUNT(*) FROM customer WHERE deleted_at IS NULL
   UNION ALL SELECT 'keys', COUNT(*) FROM api_key;"
# Expected: 0 active customers, 0 keys

# Audit log preserved (append-only)
$CE sqlite3 /var/lib/compadre/registry.db \
  "SELECT COUNT(*) FROM audit_log;"
# Expected: >0 (all historical rows preserved)
```

---

## Files to Modify

None expected during testing. If bugs are found, the fix targets are:

| Bug Location | File |
|-------------|------|
| CLI verb logic | `src/compadre/keys/cli/commands/{customers,keys,billing,system}.py` |
| Database operations | `src/compadre/keys/db.py` |
| Encryption/decryption | `src/compadre/keys/crypto.py` |
| Audit logging | `src/compadre/keys/audit.py` |
| Billing logic | `src/compadre/keys/billing.py` |
| CLI output rendering | `src/compadre/keys/cli/output.py` |
| Health collection | `src/compadre/keys/health.py` |
| Counter aggregation | `src/compadre/counters.py` |

## New Files

- `~/verify-registry.sh` on the VPS — reusable SQL verification script (transient)

## Dependencies

- **Plan 3**: Bootstrap must be complete; service running; registry initialized
- SSH access to `10.10.10.208`
- `sqlite3` available in container (included in `python:3.12-slim`)
- `jq` available on the VPS host for JSON parsing (or pipe from container)

## Risks

| Risk | Severity | Mitigation |
|------|----------|------------|
| `delete-customer --hard` permanently destroys data | High | Create dedicated disposable test customers; never delete pre-existing data |
| `rotate-encryption-key` re-encrypts all active tax-IDs | High | Take a backup before rotation; keep old `.env` value for rollback |
| Concurrent tests may stress the VPS (4 vCPU) | Low | Serialize DB-mutating tests; only light concurrent smoke tests |
| Exit code behavior may differ between CLI version and AGENTS.md | Medium | Treat AGENTS.md as source of truth; flag discrepancies as documentation bugs |
| Some edge cases are hard to reproduce (hash collisions, InvalidToken) | Low | Document which tests are manual-only; test the error messages without triggering real failures |
| `compadre-keys` available only via `docker compose exec` | Low | Ensure `docker compose exec` works; fall back to `docker exec` if needed |
| Test data bleeds into production on a shared VPS | Medium | Run this plan ONLY on the test VPS (`10.10.10.208`) with no production payloads |
| Clock-dependent tests (`mark-paid` anchoring, sweep cutoff) | Low | Check `date` on VPS; ensure `BILLING_TIMEZONE` is UTC (default); document any TZ-specific behaviors |

## Validation Checklist

### Customer Verbs
- [ ] `create-customer` with valid CPF → exit 0, customer inserted
- [ ] `create-customer` with valid CNPJ → exit 0
- [ ] `create-customer` with invalid CPF → exit 2
- [ ] `create-customer` with duplicate tax-ID → exit 2
- [ ] `create-customer` with NUL/CR/empty name → exit 2
- [ ] `create-customer` with oversized notes → exit 2
- [ ] `list-customers` table and JSON modes → correct output
- [ ] `list-customers --deleted` → shows tombstones
- [ ] `show <customer>` → panel with correct data
- [ ] `show <customer> --audit-tail 5` → audit events appended
- [ ] `show <customer> --format json` → valid JSON structure
- [ ] `show` non-existent target → exit 3
- [ ] `update-customer --add-note` → dated entry appended
- [ ] `update-customer --notes` (replace) → overwrites notes
- [ ] `update-customer --name` → name changed
- [ ] `update-customer --email` → email changed
- [ ] `update-customer` no-op (same value) → "No changes" message
- [ ] `update-customer` with NUL/newline in note → exit 2
- [ ] `update-customer` with `--add-note` + `--notes` → exit 2 (mutex)
- [ ] `update-customer` non-existent customer → exit 3

### Key Verbs
- [ ] `issue-key` manual + price → exit 0, panel displayed
- [ ] `issue-key` complimentary → exit 0, price_monthly=0.0
- [ ] `issue-key` missing --price for manual → exit 2
- [ ] `issue-key` --price for complimentary → exit 2
- [ ] `issue-key` RPM out of range → exit 2
- [ ] `issue-key` non-existent customer → exit 3
- [ ] `list-keys` with filters → correct rows
- [ ] `list-keys --format json` → valid JSON
- [ ] `show <key-prefix>` → panel with correct data
- [ ] `show <key-prefix> --audit-tail` → audit events + last-24h stats
- [ ] `show` by pasted `cmpd_live_...` → works (redacted in log)
- [ ] `update-key --rpm` → RPM changed
- [ ] `update-key --price` → price changed
- [ ] `update-key --notes` → notes changed
- [ ] `update-key --price` on complimentary key → exit 2
- [ ] `rotate-key` → new plaintext, old → 401, new → 200
- [ ] `rotate-key` preserves `key_id` (same rate-limit bucket)
- [ ] `rotate-key` revoked key → exit 3
- [ ] `rotate-key` --yes → no prompt
- [ ] `revoke-key` → exit 0, key → 401
- [ ] `revoke-key` already revoked → exit 3
- [ ] `revoke-key` empty/missing --reason → exit 2

### Billing Verbs
- [ ] `mark-paid --until <date>` → paid_until set exactly
- [ ] `mark-paid --months N` from current paid_until → extends from paid_until
- [ ] `mark-paid --months N` from expired paid_until → extends from today
- [ ] `mark-paid --price-monthly` → atomic with paid_until write
- [ ] `mark-paid` both --months + --until → exit 2
- [ ] `mark-paid` complimentary key → informational (no mutation)
- [ ] `mark-paid` revoked key → exit 3
- [ ] `suspend` → status=suspended, key → 401
- [ ] `suspend` already suspended → exit 3
- [ ] `reactivate` → status=active, key → 200
- [ ] `reactivate` already active → exit 3
- [ ] `at-risk --window Nd` → correct keys listed
- [ ] `at-risk` excludes trial keys (paid_until NULL)
- [ ] `at-risk` invalid window format → exit 2
- [ ] `revenue --month current` → correct MRR/projected
- [ ] `revenue --month YYYY-MM` → correct
- [ ] `revenue --as-of` → replay matches expected historical state
- [ ] `revenue --as-of` future date → exit 2
- [ ] `revenue --as-of` before audit → exit 2

### Data-Subject Verbs
- [ ] `export-customer` default → no audit_events key
- [ ] `export-customer --include-audit` → audit_events present, chronological
- [ ] `export-customer` decrypted tax_id matches original
- [ ] `export-customer` invalid ID shape → exit 2
- [ ] `correct-tax-id` → customer_id preserved, FKs unchanged
- [ ] `correct-tax-id` → new tax_id in export
- [ ] `correct-tax-id` duplicate tax-ID → exit 2
- [ ] `correct-tax-id` invalid new CPF → exit 2
- [ ] `delete-customer --hard` → tombstone with all contracts
- [ ] Tombstone: tax_id_encrypted = X'' (zero-length BLOB, NOT NULL)
- [ ] Tombstone: name/email/notes = ""
- [ ] Tombstone: tax_id_lookup = NULL
- [ ] Tombstone: deleted_at IS NOT NULL
- [ ] Tombstone: all api_keys DELETEd
- [ ] Tombstone: audit row written
- [ ] Tombstone: same CPF can be re-registered (new cust_ulid)
- [ ] `delete-customer --hard` + `create-customer` → different customer_id (anti-pattern confirmed)

### System Verbs
- [ ] `suspend-sweep --dry-run` → preview only, no mutation
- [ ] `suspend-sweep` → expired manual keys suspended
- [ ] `suspend-sweep` never touches complimentary keys
- [ ] `suspend-sweep` never touches NULL paid_until (trial keys)
- [ ] `suspend-sweep` uses strict `<` not `<=`
- [ ] `health --format table` → exits 0, all rows rendered
- [ ] `health --format json` → exits 0 when healthy
- [ ] `health --format json` → exits 7 when any warn/fail
- [ ] `cache-status` → shows TTL, propagation bound, live stats
- [ ] `rotate-encryption-key` → valid Fernet key, re-encrypts active only
- [ ] `rotate-encryption-key` → tombstones skipped
- [ ] `rotate-encryption-key` invalid key shape → exit 4
- [ ] `rotate-encryption-key` round-trip: export decrypts after .env update

### Exit Codes
- [ ] Exit 0 verified (any successful command)
- [ ] Exit 2 verified (≥3 different conditions)
- [ ] Exit 3 verified (≥3 different conditions)
- [ ] Exit 4 verified (invalid Fernet key)
- [ ] Exit 5 verified (DB error)
- [ ] Exit 6 verified (missing encryption key or invalid TZ)
- [ ] Exit 7 verified (health --format json with warn/fail)

### Invariants
- [ ] Single-Fernet-call: `fernet.decrypt` only in `crypto.py`
- [ ] No raw `HTTPException` in `src/compadre/`
- [ ] No raw `os.environ` in `src/compadre/`
- [ ] No `print()` in core application code
- [ ] Backup naming: `*.backup-*.tar.gz.enc`
- [ ] `_FORBIDDEN_DETAIL_KEYS` enforced at write + read
- [ ] Import graph flows downward only
- [ ] Counter snapshots: file exists, valid JSON
- [ ] Tombstone shape correct (all 6 contract checks)
- [ ] No PII in audit `details_json`

### Concurrent Access
- [ ] `issue-key` during active traffic → no corruption
- [ ] `rotate-key` during active traffic → no corruption, cache propagation OK

---

## Test Log Template

Record results in a companion file `docs/plan/plan-04-test-log.md`:

```markdown
# Plan 4 Test Log

**Date:** 2026-04-27
**VPS:** 10.10.10.208
**Tester:** [name]

## Task 2 — Customer Verbs

| Test | Expected | Actual | Exit | Pass? | Notes |
|------|----------|--------|------|-------|-------|
| 2.1 create-customer CPF | exit 0, customer created | ... | 0 | ✅ | |
| 2.2 create-customer invalid CPF | exit 2, "CPF inválido" | ... | 2 | ✅ | |
| ... | ... | ... | ... | ... | |
```
