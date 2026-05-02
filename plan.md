# Implementation Plan: Eliminate Local-File Dependency in restore-keys.yml

## Independent Review

**Reviewer**: reviewer subagent (independent verification against live files).  
**Status**: Plan is correct and complete. Three clarifications added below.

| # | Original Plan Item | Review Finding |
|---|-------------------|----------------|
| 1 | `gather_facts: true` needed for `ansible_facts['env']['HOME']` | **Verified correct**. `slurp.src` does not expand `~`; it requires an absolute path. `bootstrap.yml` uses the same pattern at line 308 (`ansible_facts['env']['HOME']` / `compadre/deploy/caddy/Caddyfile`). `update.yml` also uses `gather_facts: true`. |
| 2 | `slurp` + `b64decode` replaces `lookup('file', ...)` | **Verified correct**. `ansible.builtin.slurp` reads from the *managed node* (VPS) by design. Unlike `copy` or `template` which default to control-node source, `slurp` has no `remote_src` toggle because it inherently operates remotely. The `b64decode` filter is standard Jinja2 (available since Ansible 2.0). |
| 3 | `no_log: true` on shell task still protects content | **Verified correct, with clarification**. The `slurp` tasks themselves do NOT need `no_log: true` — the scripts are public shell code (versioned in the repo, not secrets). Only the `[Stage] Decrypt + untar + integrity-check` shell task needs `no_log: true` because it handles `BACKUP_PASSPHRASE` in its `environment`. The `stdin` change does not affect this. |
| 4 | F6 comment about base64 round-trip | **Verified: comment becomes accurate**. The *current* comment mentions a base64 round-trip that the *current* code does not perform. With `slurp` + `b64decode`, the code **does** perform a base64 round-trip (slurp returns base64, b64decode decodes). The updated comment should reflect this accurately. |
| 5 | Line numbers for insertions (before line 111, before line 165) | **Approximate but correct**. The stage task name is at line 113; the swap block starts at line 169. The plan's insertion points place new tasks in the right logical positions (before the tasks that consume the slurped variables). |
| 6 | Risk #6 — stale VPS clone | **Valid and important**. `update.yml` runs `ansible.builtin.git` with `force: true, update: true` (verified in `update.yml` lines 54–61). The pre-flight recommendation is actionable. |

**No blockers found.**

---

## Goal

Remove the `lookup('file', playbook_dir + ...)` calls from `restore-keys.yml` so the disaster-recovery playbook reads scripts from the VPS's own git clone instead of the developer's laptop. Any machine with Ansible and the required env vars can then run the disaster-recovery playbook.

## Context

The VPS **already has a full git clone** at `~/compadre` (created by `bootstrap.yml`'s `[Git] Clone Compadre repository` task at ~line 282, with `force: true, update: true`). The two scripts are already on the VPS at:

- `~/compadre/deploy/ansible/scripts/restore-stage.sh`
- `~/compadre/deploy/ansible/scripts/restore-swap.sh`

We only need to change _how_ the playbook reads them — from Ansible control-node `lookup('file', ...)` to remote-host `slurp`.

## Why `slurp` (not `copy` or `template`)

- `ansible.builtin.copy` and `ansible.builtin.template` default to reading from the **control node** (the developer's laptop). `remote_src: true` can be used with `copy`, but that copies a file from remote path A to remote path B — it does not return file content as a variable.
- `ansible.builtin.slurp` reads a file from the **managed node** (the VPS) and returns its content as a **base64-encoded string** in a registered variable. The `b64decode` Jinja2 filter then decodes it for use in `stdin` or `command`.
- This matches the project's existing pattern: `bootstrap.yml` already uses `ansible_facts['env']['HOME']` to reference paths inside `~/compadre` on the VPS (line 308).

## Pre-flight for Operators

Before running the modified `restore-keys.yml`, ensure the VPS clone is fresh:

```bash
ansible-playbook playbooks/update.yml -i inventory/production.yml
```

This runs `[Git] Pull latest changes` with `update: true` which does `git pull` on the VPS clone, ensuring the scripts match the latest repo version. If `update.yml` is skipped and the clone is stale, the playbook will run outdated scripts — silently wrong behavior. See Risk #6.

## Tasks

### 1. Enable fact gathering in restore-keys.yml

- **File**: `deploy/ansible/playbooks/restore-keys.yml`
- **Change**: Line 28: replace `gather_facts: false` with `gather_facts: true`
- **Why**: `slurp` needs an absolute path (it does not expand `~`). The bootstrap.yml pattern at line 308 uses `ansible_facts['env']['HOME']`. `update.yml` also uses `gather_facts: true`. Fact gathering adds ~1–2s — negligible for a DR playbook that takes minutes.
- **Acceptance**: `ansible_facts['env']['HOME']` resolves to e.g. `/home/<ansible_user>`.

### 2. Slurp restore-stage.sh from VPS clone, then pipe via stdin

- **File**: `deploy/ansible/playbooks/restore-keys.yml`
- **Changes**:
  - **Add** a new task immediately before the `[Stage] Decrypt + untar + integrity-check` task (currently around line 111–113):
    ```yaml
    - name: "[Stage] Read restore-stage script from VPS clone"
      ansible.builtin.slurp:
        src: "{{ ansible_facts['env']['HOME'] }}/compadre/deploy/ansible/scripts/restore-stage.sh"
      register: stage_script_slurp
    ```
  - **Modify** line 125: replace
    ```yaml
    stdin: "{{ lookup('file', playbook_dir + '/../scripts/restore-stage.sh') }}"
    ```
    with
    ```yaml
    stdin: "{{ stage_script_slurp.content | b64decode }}"
    ```
  - **Keep** `no_log: true` on the shell task — it still hides task output that may contain `BACKUP_PASSPHRASE` via the `environment` block, just as it did before.
  - **Keep** `# noqa: command-instead-of-shell` — the task is still `ansible.builtin.shell`, only the `stdin` source changed.
  - **Note**: The `slurp` task itself does not need `no_log: true`; the script content is public shell code (versioned in the repository).
- **Acceptance**: The `bash -s` inside `docker compose exec` receives the same script content as before, but sourced from the VPS rather than the control node.
- **Dependency**: Task 1 (needs `ansible_facts['env']['HOME']`).

### 3. Slurp restore-swap.sh from VPS clone, then embed in container command

- **File**: `deploy/ansible/playbooks/restore-keys.yml`
- **Changes**:
  - **Add** a new task immediately before the `[Restore] Swap DB file via helper container` block (currently around line 165–169):
    ```yaml
    - name: "[Restore] Read restore-swap script from VPS clone"
      ansible.builtin.slurp:
        src: "{{ ansible_facts['env']['HOME'] }}/compadre/deploy/ansible/scripts/restore-swap.sh"
      register: swap_script_slurp
    ```
  - **Modify** line 184: replace
    ```yaml
    - "{{ lookup('file', playbook_dir + '/../scripts/restore-swap.sh') }}"
    ```
    with
    ```yaml
    - "{{ swap_script_slurp.content | b64decode }}"
    ```
- **Acceptance**: The alpine helper container's `sh -c` receives the same script content as before, sourced from the VPS.
- **Dependency**: Task 1 (needs `ansible_facts['env']['HOME']`).

### 4. Update F6 comments

- **File**: `deploy/ansible/playbooks/restore-keys.yml`
- **Changes**:
  - **Lines 107–112** (F6 comment before the stage task): Replace:
    ```yaml
    # F6 — script body lives in deploy/ansible/scripts/restore-stage.sh;
    # piped into ``bash -s`` inside the container. ``$BACKUP_FILE`` is
    # passed via -e so the script reads it via shell parameter expansion
    # (F4 — no raw jinja interpolation into a shell command).
    ```
    with:
    ```yaml
    # F6 — script body is slurped from the VPS git clone at
    # ~/compadre/deploy/ansible/scripts/restore-stage.sh, base64-decoded,
    # and piped into ``bash -s`` inside the container.
    # No control-node file dependency.
    # ``$BACKUP_FILE`` is passed via -e so the script reads it via shell
    # parameter expansion (F4 — no raw jinja interpolation into a shell command).
    ```
  - **Lines 173–176** (F6 comment inside the swap block): Replace:
    ```yaml
    # F6 — script body lives in deploy/ansible/scripts/restore-swap.sh.
    # The alpine helper container runs ``sh -c "$(cat ... | base64 -d)"``
    # — passing the script content through a base64 round-trip
    # sidesteps the docker_container ``command`` field's quoting layer
    # without re-introducing the inline jinja blob.
    ```
    with:
    ```yaml
    # F6 — script body is slurped from the VPS git clone at
    # ~/compadre/deploy/ansible/scripts/restore-swap.sh, base64-decoded,
    # and embedded in the docker_container ``command`` field.
    # The slurp → b64decode round-trip sidesteps the quoting layer
    # without an inline jinja blob and without a control-node file dependency.
    ```
- **Acceptance**: Comments accurately describe the slurp-from-VPS + base64-decode mechanism. The base64 round-trip is now actually performed (by `slurp` + `b64decode`), making the comment accurate where the old comment was aspirational.

### 5. Validate with CI/CD

- **Actions**:
  - Run `ansible-playbook --syntax-check deploy/ansible/playbooks/restore-keys.yml -i deploy/ansible/inventory/production.yml` locally
  - Run `ansible-lint deploy/ansible/playbooks/restore-keys.yml` locally
  - Push the branch; CI `ansible-lint` job catches regressions
  - Run full `ci-local.sh` to catch cross-playbook regressions
- **Acceptance**: All lint, syntax, and CI checks pass with zero new warnings.

### 6. Manual verification on staging VPS

- **Action**: Run the modified `restore-keys.yml` against a test VPS with a real backup archive
- **Acceptance**: The restore completes successfully. The summary reports the correct archive path and key-divergence state, identical to the current behavior.

## Files to Modify

- **`deploy/ansible/playbooks/restore-keys.yml`** — the ONLY file changed:
  - Line 28: `gather_facts: false` → `gather_facts: true`
  - Before the `[Stage] Decrypt + untar + integrity-check` task (around line 111–113): add slurp task for `restore-stage.sh`
  - Around lines 107–112: update F6 comment for stage
  - Line 125: `lookup('file', ...)` → `stage_script_slurp.content | b64decode`
  - Before the `[Restore] Swap DB file via helper container` block (around line 165–169): add slurp task for `restore-swap.sh`
  - Around lines 173–176: update F6 comment for swap
  - Line 184: `lookup('file', ...)` → `swap_script_slurp.content | b64decode`

## New Files

None.

## Deleted Files

None.

## Dependencies

```
Task 1 (gather_facts) ─┬─→ Task 2 (slurp stage) ─┐
                       └─→ Task 3 (slurp swap) ──┤
                                                   ├→ Task 4 (comments)
                                                   └→ Task 5 (CI)
                                                        └→ Task 6 (manual test)
```

Tasks 2 and 3 are independent and can be done in either order. Task 4 (comments) is independent of 2/3 but depends on agreeing on the final mechanism.

## Risks

| # | Risk | Likelihood | Impact | Mitigation |
|---|------|-----------|--------|-----------|
| 1 | `gather_facts: true` times out on degraded VPS | Low | Low — DR can't proceed anyway | Fact gathering is ~1–2s on healthy hosts. If the VPS is too degraded to gather facts, the restore likely can't proceed. |
| 2 | `ansible_facts['env']['HOME']` differs from expected home | Very low | Medium — slurp fails | `bootstrap.yml` uses the same pattern at line 308. The `[Prereq] Verify ~/compadre exists` task catches a missing directory before any slurp runs. |
| 3 | Script content corrupted by b64decode | Very low | High — restore runs corrupted script | `slurp` returns base64; `b64decode` is a standard Jinja filter. Scripts are plain ASCII/UTF-8 bash. Identical to current `lookup('file', ...)` path. |
| 4 | Swap script hits quoting issues when embedded via b64decode | Very low | Medium — container fails | The script is a single list element in `command:`. `docker_container` passes it as one argument to `sh -c`. No shell word-splitting. Same content as before. |
| 5 | Ansible version incompatibility | Very low | High — playbook fails | `slurp` and `b64decode` available since Ansible 2.0. Project already targets modern Ansible. |
| 6 | **VPS clone is stale** (operator hasn't run `update.yml`) | Medium | High — restore uses outdated scripts silently | **New risk.** The playbook has no guard against clone staleness. Operators MUST run `update.yml` before a DR restore. Documented in Pre-flight section above. A future enhancement could add a `git fetch --dry-run` check that warns (not blocks) on divergence. |

## Edge Cases & Notes

- **Script size**: Both scripts are small (~80 and ~20 lines). `slurp` reads the entire file into memory. This is safe — no streaming or chunking needed.
- **Registered variables**: `stage_script_slurp` and `swap_script_slurp` contain base64-encoded content. They are visible in Ansible verbose output (`-vvv`). This is not a security issue — the scripts are public repository code.
- **Idempotency**: The playbook remains idempotent. `slurp` is a read-only operation. The stage and swap logic is unchanged.
- **Backup-keys.yml unaffected**: `backup-keys.yml` uses inline `stdin: |` (no `lookup`). No changes needed there.
- **Future alternative**: If the project ever moves to GitHub raw URLs or an artifact store, the slurp tasks could be replaced with `ansible.builtin.uri` fetching from a stable URL. Not needed in this plan.
