# Compadre Deploy Report — 2026-04-28

**Target VPS:** 10.10.10.208 (Debian 13.4, x86_64)
**User:** galvani (SSH key auth)
**Ansible:** core 2.20.3, community.docker 5.0.6, community.general 11.2.1

## Playbook Execution Summary

| # | Playbook | Result | Tasks | Failed |
|---|----------|--------|-------|--------|
| 1 | `cleanup.yml` | ✅ PASS | 23 ok | 0 |
| 2 | `bootstrap.yml` | ✅ PASS | 42 ok | 0 |
| 3 | `deploy-models.yml` | ✅ PASS | 9 ok | 0 |
| 4 | `health.yml` (1st run) | ❌ FAIL | 24 ok | 1 |
| 5 | `health.yml` (2nd run) | ✅ PASS | 31 ok | 0 |
| 6 | `backup-keys.yml` | ✅ PASS | 12 ok | 0 |
| 7 | `update.yml` | ✅ PASS | 10 ok | 0 |
| 8 | `restore-keys.yml` | ✅ PASS | 18 ok | 0 |
| 9 | `health.yml` (final) | ✅ PASS | 33 ok | 0 |

## Error Found & Fixed

### Error 1: health.yml UFW check fails without become password

**Playbook:** `health.yml`
**Task:** `[UFW] Assert UFW is active`
**Symptom:** `health.yml` failed on first run because the unprivileged `ufw status` returns empty/incomplete output on Debian, and the sudo fallback requires `ansible_become_password`.

**Root cause:** The playbook has a two-tier UFW read (unprivileged → sudo fallback), but the sudo fallback needs `become: true` which requires the become password passed via `-e`.

**Fix:** Re-run `health.yml` with `-e "ansible_become_password=davi2019"`:
```bash
ansible-playbook playbooks/health.yml -i inventory/production.yml \
  -e "ansible_become_password=davi2019"
```

**Status:** Fixed. Second run passed all 31 checks.

## Final State

```
Replicas:         1
DB file mode:     600 ✅
DB parent mode:   700 ✅
Crons installed:  compadre-daily-suspension ✅, compadre-keys-backup ✅
Backup archives:  1 present ✅
UFW:              active (6 ALLOW lines: 22, 80, 443) ✅
compadre-keys health: 10 checks, all green ✅
/health status:   healthy ✅
Disk usage:       40% ✅
```

### API Transcription Test

- Audio: `audio-de-teste-01.ogg` (14.3s, Brazilian Portuguese)
- Output: ✅ Full transcription with word timestamps, segments, and language detection
- Formats tested: `text`, `verbose_json`

### Backup & Restore

- Backup: `registry.db.backup-2026-04-28T18-44-18Z.tar.gz.enc` (1573 bytes, mode 600, AES-256-GCM)
- Restore: ✅ Full cycle (decrypt → verify → stop → swap → restart → healthy)

## Conclusion

**6 de 7 playbooks executaram de primeira, sem qualquer erro.** Apenas `health.yml` precisou de uma segunda execução com a senha sudo para o check do UFW — um requisito conhecido da arquitetura em Debian onde `ufw status` requer privilégios.

O serviço está funcionando perfeitamente: transcrição STT, gestão de chaves, backup/restore, e todos os checks de segurança (permissões 0600/0700, UFW whitelist, crons ativos).
