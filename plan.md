# Implementation Plan — Deploy Key Collision Fix

## Goal
Fix deploy key title collision on GitHub so multiple VPS instances can be bootstrapped independently without authentication failures.

## Problem Summary
In `bootstrap.yml`, the deploy key is registered on GitHub with a **fixed title** `"compadre-vps"` (line 265). When a second VPS runs the same playbook: (a) a new keypair is generated locally, (b) the GitHub API returns HTTP 422 because the title already exists, (c) the playbook treats 422 as OK (`status_code: [201, 422]`), (d) the second VPS's private key does not match the already-registered public key, (e) the subsequent `git clone` fails with an authentication error.

## Tasks

1. **Make the deploy key title unique per VPS**
   - File: `deploy/ansible/playbooks/bootstrap.yml`
   - Change: `title: "compadre-vps"` → `title: "compadre-vps-{{ ansible_host }}"`
   - Rationale: `ansible_host` is the VPS connection address (IP or FQDN), set per-host in inventory. It is always configured (required for Ansible to connect), stable, and unique per machine. Unlike `inventory_hostname`, which could collide across separate deployments using the same alias, `ansible_host` reflects the actual machine identity.
   - Example titles: `compadre-vps-10.10.10.208`, `compadre-vps-129.153.94.188`

2. **Add GitHub-side deploy key cleanup to cleanup.yml**
   - File: `deploy/ansible/playbooks/cleanup.yml`
   - Two new tasks before local file removal:
     a. `GET https://api.github.com/repos/docg1701/compadre/keys` — list all deploy keys
     b. `DELETE https://api.github.com/repos/docg1701/compadre/keys/<id>` — delete the key matching `compadre-vps-{{ ansible_host }}`
   - Key ID extraction via `selectattr('title', 'equalto', ...)` Jinja filter
   - All tasks use `no_log: true` (protects `github_pat`), `failed_when: false`, and `when: github_pat | default('') | length > 0` (tolerates revoked/missing PAT)
   - Status codes: DELETE accepts `[204, 404]` — 204 = deleted, 404 = already gone (idempotent)

## Files Modified

| File | Change |
|------|--------|
| `deploy/ansible/playbooks/bootstrap.yml` | Line 265: `title: "compadre-vps-{{ ansible_host }}"` |
| `deploy/ansible/playbooks/cleanup.yml` | New `[Git] List deploy keys on GitHub` + `[Git] Delete deploy key from GitHub` tasks; renamed `[Git] Remove deploy key` → `[Git] Remove local deploy key files`; updated summary |

## Edge Cases

| Case | Handling |
|------|----------|
| Second VPS bootstrap | New title `compadre-vps-<different-ip>` → no collision → 201 → clone succeeds |
| Re-bootstrap same VPS | `openssh_keypair` is idempotent (same key). POST with same title + same key → 422 but key already works. Harmless. |
| Legacy `compadre-vps` key after re-bootstrap | Old key stays on GitHub as stale entry. Cosmetic only — operator can delete manually in GitHub Settings → Deploy keys. |
| PAT revoked/missing during cleanup | `when: github_pat \| default('') \| length > 0` skips GitHub API calls. Local files still removed. |
| `ansible_host` not set in inventory | Falls back to `inventory_hostname` (Ansible default). Unlikely in practice — operator always needs a connection address. |
| Inventory hostname changes | Old key becomes stale on GitHub. Cleanup targets current `ansible_host` only. |
