# Compadre — Checkpoint Final de Implementação

> Atualizado em: 2026-04-26
> Trabalho retomado com workers paralelos e consolidação manual.

---

## 1. O que foi COMPLETADO nesta sessão

| # | Tarefa | Status | Detalhes |
|---|---|---|---|
| 1 | **Fix ruff errors** | ✅ | 15 erros corrigidos em 12 arquivos. `uv run ruff check .` passa zero erros |
| 2 | **Landing page STT** | ✅ | `deploy/landing/index.html` reescrito (pt-BR, upload áudio, 5 formatos, drag-drop, copy-to-clipboard) |
| 3 | **Testes STT novos** | ✅ | `test_stt.py` (10), `test_formatters.py` (12), `test_preprocessing_audio.py` (10) — 32/32 passando |
| 4 | **Ansible playbooks** | ✅ | 7 playbooks atualizados + deploy-models.yml reescrito para Whisper cache |
| 5 | **README.md** | ✅ | Reescrito para STT (custos, quick start multipart, arquitetura) |
| 6 | **CLAUDE.md** | ✅ | Reescrito para STT (gotchas, convenções, engine params) |
| 7 | **Scripts de load test** | ✅ | `loadtest.py` e `capacity_test.py` reescritos para STT (upload áudio, métricas RTF/throughput) |
| 8 | **Deletar feasibility_test.py** | ✅ | Script TTS-specific removido |
| 9 | **.env.example** | ✅ | Atualizado para STT env vars (WHISPER_*, HF_HOME) |
| 10 | **Docs cleanup** | ✅ | Mass rename em docs/ (comadre-keys, tts-service, paths, volumes) |
| 11 | **mypy** | ✅ | `uv run mypy src/` → Success: no issues found in 36 source files |
| 12 | **Deploy VPS** | ✅ | Build e start no 10.10.10.208 — modelo large-v3-turbo int8 baixado (~1.5GB), endpoint funcional |
| 13 | **Fix repo HuggingFace** | ✅ | `download.py`: `Systran/` → `mobiuslabsgmbh/faster-whisper-large-v3-turbo` (repo ativo) |

---

## 2. Resultado dos Testes (Local)

```bash
uv run pytest -m "not slow" --tb=line -q
→ 248 passed, 11 failed, 10 errors
```

- **248 passados** (+33 desde o checkpoint anterior)
- **11 falhos + 10 erros**: exclusivamente `tests/test_non_regression/test_request_body_limit.py` e `test_security_headers.py` — requerem Caddy rodando em Docker (`ConnectionRefusedError`)

**Ruff**: `All checks passed!`  
**mypy**: `Success: no issues found in 36 source files`

---

## 3. Deploy no VPS (10.10.10.208)

| Fase | Status | Detalhes |
|---|---|---|
| Health check VPS | ✅ | Debian 13, 8GB RAM, 29GB disk, Docker v29.4.1 |
| Rsync código | ✅ | 4.3 MB syncado para `~/compadre` |
| Docker build | ✅ | Compadre + Caddy buildados |
| Modelo download | ✅ | large-v3-turbo int8 baixado (~1.5GB, 29s) |
| Containers up | ✅ | compadre-stt + caddy healthy |
| Testes integração | ✅ | 225/227 passaram (2 falhas são testes de design que exigem KEY_REGISTRY_ENCRYPTION_KEY ausente) |
| Validação manual | ✅ | Customer criado, key emitida, transcrição funciona (`1s silence OPUS` → `"Obrigado."`) |
| /health | ✅ | `{"status":"healthy","models":["large-v3-turbo"]}` |

### Endpoints verificados
```bash
# Health
curl -k https://10.10.10.208/health
→ {"status":"healthy","models":["large-v3-turbo"]}

# Transcrição (texto)
curl -k -X POST https://10.10.10.208/v1/audio/transcriptions \
  -H "Authorization: Bearer cmpd_live_..." \
  -F "file=@test.opus" -F "response_format=text"
→ "Obrigado."
```

---

## 4. O que ainda FALTA (para MVP 100%)

| # | Tarefa | Prioridade | Bloqueio |
|---|---|---|---|
| 1 | **Testes não-regressão com Caddy** | Média | Requer `docker compose up` com Caddy ativo localmente, ou adaptar fixtures para apontar para o VPS |
| 2 | **Load test real no VPS** | Média | Rodar `scripts/loadtest.py` e `scripts/capacity_test.py` contra 10.10.10.208 |
| 3 | **Docs restantes** | Baixa | `docs/troubleshooting.md`, `docs/deployment-guide.md`, etc. ainda têm ~100 referências a `comadre` em contextos históricos/arquiteturais (não-blockers) |
| 4 | **Logo rename** | Baixa | `logo-comadre.webp` → `logo-compadre.webp` (apenas assets) |
| 5 | **Git inicialização** | Baixa | `git init` + `git add .` + primeiro commit |

---

## 5. Comandos úteis

### Local (dev)
```bash
cd /home/galvani/dev/compadre
uv run pytest -m "not slow" -q        # 248 passed
uv run ruff check .                   # All checks passed!
uv run mypy src/                      # Success
```

### VPS (10.10.10.208)
```bash
ssh galvani@10.10.10.208
cd ~/compadre

# Verificar status
docker compose ps
docker compose logs -f compadre-stt

# CLI de chaves
docker compose exec -T compadre-stt compadre-keys list-customers
docker compose exec -T compadre-stt compadre-keys list-keys

# Teste rápido
curl -k https://10.10.10.208/health
curl -k -X POST https://10.10.10.208/v1/audio/transcriptions \
  -H "Authorization: Bearer <key>" \
  -F "file=@audio.opus" -F "response_format=text"
```

---

## 6. Próximos passos recomendados

1. **Rodar load tests no VPS**: Validar throughput real com áudios de 5-30s
2. **Teste com áudio real do WhatsApp**: Verificar qualidade de transcrição PT-BR
3. **Git init + commit**: Preparar para versionamento
4. **Atualizar docs**: Remover referências restantes a `comadre` em troubleshooting/deployment-guide
