#!/bin/sh
# Compadre restore — SWAP step. Runs in an alpine helper container while
# tts-service is stopped. Removes WAL/SHM sidecars (invalidated by the
# swap), moves the staged registry.db into place, and chmod 0600.
#
# Driven by deploy/ansible/playbooks/restore-keys.yml's
# "[Restore] Create helper container" task. F6 — externalised from the
# inline ``_swap_script`` jinja blob.

set -eu
umask 077
DB_PATH="/var/lib/compadre/registry.db"
STAGING="/var/lib/compadre/.restore-staging"
rm -f "$DB_PATH-wal" "$DB_PATH-shm"
mv "$STAGING/registry.db" "$DB_PATH"
chmod 0600 "$DB_PATH"
rm -rf "$STAGING"
echo "swap OK"
