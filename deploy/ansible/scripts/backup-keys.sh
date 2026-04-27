#!/usr/bin/env bash
# Compadre nightly backup — runs INSIDE the compadre-stt container via
# ``docker compose exec``. Driven by deploy/ansible/playbooks/backup-keys.yml.
#
# Required env (set by the playbook's ``-e`` flags):
#   BACKUP_PASSPHRASE       — operator passphrase (>=16 chars).
#   BACKUP_RETENTION_DAYS   — integer, archive prune window.
#
# Required env (set by the docker compose service definition):
#   KEY_REGISTRY_ENCRYPTION_KEY  — Fernet key. Captured into the tarball
#                                  so a restore can decrypt the archive
#                                  even if .env has rotated since.
#   KEY_REGISTRY_DB_PATH (default /var/lib/compadre/registry.db).
#
# F6 — externalised from the inline ``_backup_script`` jinja blob so a
# future edit cannot silently break under quote-fragility. Body is
# load-bearing — see backup-keys.yml's preamble for the contract.

set -euo pipefail
umask 077

# Belt-and-braces: fail loud if the env var is absent. Bootstrap
# already requires it at service-start, but a future helper-container
# path that bypasses bootstrap inherits the guarantee here.
: "${KEY_REGISTRY_ENCRYPTION_KEY:?KEY_REGISTRY_ENCRYPTION_KEY must be set inside the compadre-stt container}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE must be set via -e}"
: "${BACKUP_RETENTION_DAYS:?BACKUP_RETENTION_DAYS must be set via -e}"

DB_PATH="${KEY_REGISTRY_DB_PATH:-/var/lib/compadre/registry.db}"
DB_DIR="$(dirname "$DB_PATH")"
TS="$(date -u +%Y-%m-%dT%H-%M-%SZ)"
STAGING="$(mktemp -d -t compadre-backup.XXXXXX)"
# shellcheck disable=SC2064
# Expand $STAGING NOW so the trap captures the exact path even after
# subsequent script logic changes $STAGING.
trap "rm -rf \"$STAGING\"" EXIT

python -c "
import sqlite3, sys
# 30s lock timeout — generous over SQLite's 5s default so a busy
# registry under a concurrent CLI write doesn't flake the snapshot.
src = sqlite3.connect(sys.argv[1], timeout=30)
dst = sqlite3.connect(sys.argv[2], timeout=30)
with dst:
    src.backup(dst)
src.close()
dst.close()
" "$DB_PATH" "$STAGING/registry.db"

printf "%s" "$KEY_REGISTRY_ENCRYPTION_KEY" > "$STAGING/encryption-key.txt"
chmod 0600 "$STAGING/encryption-key.txt"
tar -C "$STAGING" -czf "$STAGING/bundle.tar.gz" registry.db encryption-key.txt

ARCHIVE="$DB_DIR/registry.db.backup-$TS.tar.gz.enc"

# F2 — AES-256-GCM (authenticated) via Python cryptography library.
# Archive layout (binary):
#   bytes 0..4   : magic header ``CMV2\n`` (5 bytes; ``CMV1`` would be
#                  the legacy openssl-CBC ``Salted__`` framing — the
#                  restore path detects either and dispatches).
#   bytes 5..20  : 16-byte salt for PBKDF2-HMAC-SHA256 (600 000 iters).
#   bytes 21..32 : 12-byte AES-GCM nonce (random per archive).
#   bytes 33..   : AES-256-GCM ciphertext + 16-byte auth tag (appended
#                  by ``cryptography.hazmat.primitives.ciphers.aead``).
#
# Authenticated encryption gives us tamper detection for free: a
# byte-flip in the archive causes ``aesgcm.decrypt`` to raise
# ``InvalidTag``, where the prior CBC framing would have happily
# returned garbled plaintext that ``tar`` later choked on.
python - "$STAGING/bundle.tar.gz" "$ARCHIVE" <<'PYTHON'
import os, secrets, sys
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

passphrase = os.environ["BACKUP_PASSPHRASE"].encode("utf-8")
salt = secrets.token_bytes(16)
kdf = PBKDF2HMAC(
    algorithm=hashes.SHA256(),
    length=32,
    salt=salt,
    iterations=600_000,
)
key = kdf.derive(passphrase)
nonce = secrets.token_bytes(12)
aesgcm = AESGCM(key)
with open(sys.argv[1], "rb") as fh:
    plaintext = fh.read()
ciphertext = aesgcm.encrypt(nonce, plaintext, None)
with open(sys.argv[2], "wb") as fh:
    fh.write(b"CMV2\n")
    fh.write(salt)
    fh.write(nonce)
    fh.write(ciphertext)
PYTHON
chmod 0600 "$ARCHIVE"

find "$DB_DIR" -maxdepth 1 -type f \
  -name "registry.db.backup-*.tar.gz.enc" \
  -mtime +"$BACKUP_RETENTION_DAYS" -print -delete

BYTES="$(stat -c '%s' "$ARCHIVE")"
echo "compadre-backup OK archive=$ARCHIVE bytes=$BYTES"
