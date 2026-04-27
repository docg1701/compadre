#!/usr/bin/env bash
# Compadre restore — STAGE step. Decrypt the operator-supplied backup
# archive into a staging directory inside the compadre-registry volume,
# integrity-check the SQLite DB, and surface key-divergence to the
# playbook.
#
# Required env (set by the playbook's ``-e`` flags):
#   BACKUP_FILE         — path to the .tar.gz.enc archive (operator-supplied;
#                         passed as env-var, not raw shell interpolation, so a
#                         hostile path cannot inject commands).
#   BACKUP_PASSPHRASE   — operator passphrase used at backup time.
#
# Required env (set by the docker compose service definition):
#   KEY_REGISTRY_DB_PATH (default /var/lib/compadre/registry.db).
#   KEY_REGISTRY_ENCRYPTION_KEY — current Fernet key. Compared against
#                                 the archived key inside the tarball;
#                                 divergence is announced via stdout.
#
# F6 — externalised from the inline ``_stage_script`` jinja blob.

set -euo pipefail
umask 077

: "${BACKUP_FILE:?BACKUP_FILE must be set via -e}"
: "${BACKUP_PASSPHRASE:?BACKUP_PASSPHRASE must be set via -e}"

DB_PATH="${KEY_REGISTRY_DB_PATH:-/var/lib/compadre/registry.db}"
DB_DIR="$(dirname "$DB_PATH")"
STAGING="$DB_DIR/.restore-staging"
rm -rf "$STAGING"
mkdir -p "$STAGING"
chmod 0700 "$STAGING"

# F2 — dispatch on the archive's first bytes:
#   ``CMV2\n``     → AES-256-GCM (current backups).
#   ``Salted__``   → AES-256-CBC + PBKDF2 (legacy ``openssl enc``-only
#                   archives, kept readable so 2.0.0-era backups don't
#                   require operator intervention).
# B23 — `xxd` is not in the slim Python compadre-stt image; use `od`
# (coreutils, always present) instead so restore-stage works without
# adding vim-common just for hex dumping eight bytes.
HEADER_BYTES="$(head -c 8 "$BACKUP_FILE" | od -An -tx1 | tr -d ' \n')"
if [ "${HEADER_BYTES:0:10}" = "434d56320a" ]; then
    # CMV2\n — AES-256-GCM.
    python - "$BACKUP_FILE" "$STAGING/bundle.tar.gz" <<'PYTHON'
import os, sys
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

passphrase = os.environ["BACKUP_PASSPHRASE"].encode("utf-8")
with open(sys.argv[1], "rb") as fh:
    magic = fh.read(5)
    if magic != b"CMV2\n":
        sys.exit(f"Unexpected magic header: {magic!r}")
    salt = fh.read(16)
    nonce = fh.read(12)
    ciphertext = fh.read()
kdf = PBKDF2HMAC(
    algorithm=hashes.SHA256(),
    length=32,
    salt=salt,
    iterations=600_000,
)
key = kdf.derive(passphrase)
plaintext = AESGCM(key).decrypt(nonce, ciphertext, None)
with open(sys.argv[2], "wb") as fh:
    fh.write(plaintext)
PYTHON
elif [ "${HEADER_BYTES:0:16}" = "53616c7465645f5f" ]; then
    # Salted__ — legacy openssl-CBC framing.
    openssl enc -d -aes-256-cbc -pbkdf2 -iter 600000 \
      -in "$BACKUP_FILE" \
      -out "$STAGING/bundle.tar.gz" \
      -pass env:BACKUP_PASSPHRASE
else
    echo "Unknown backup archive format (header: $HEADER_BYTES)" >&2
    exit 1
fi
tar -C "$STAGING" -xzf "$STAGING/bundle.tar.gz"
test -f "$STAGING/registry.db" \
  || { echo "Archive missing registry.db" >&2; exit 1; }
test -f "$STAGING/encryption-key.txt" \
  || { echo "Archive missing encryption-key.txt" >&2; exit 1; }

python -c "
import sqlite3, sys
conn = sqlite3.connect(sys.argv[1])
result = conn.execute('PRAGMA integrity_check').fetchone()[0]
conn.close()
sys.exit(0 if result == 'ok' else 1)
" "$STAGING/registry.db"

ARCHIVED_KEY="$(cat "$STAGING/encryption-key.txt")"
if [ "$ARCHIVED_KEY" != "$KEY_REGISTRY_ENCRYPTION_KEY" ]; then
    echo "KEY_DIVERGES"
    echo "ARCHIVED_KEY=$ARCHIVED_KEY"
else
    echo "KEY_MATCHES"
fi
rm -f "$STAGING/bundle.tar.gz"
echo "stage OK"
