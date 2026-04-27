FROM python:3.12-slim

# System dependencies: ffmpeg for audio decoding, plus openssl (AES-256-CBC
# envelope for backup/restore playbooks — Story 4.3) and the sqlite3 CLI
# (operator-debug convenience on a disaster-recovery day; not invoked by any
# playbook or Python code).
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ffmpeg \
        openssl \
        sqlite3 \
    && rm -rf /var/lib/apt/lists/*

# Install uv for Python dependency management
COPY --from=ghcr.io/astral-sh/uv:0.8.3 /uv /usr/local/bin/uv

# Copy project files for dependency installation
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
COPY src/ src/

# Install Python dependencies (production only)
RUN uv sync --frozen --no-dev

# Bake the backup script into the image as a real binary on PATH (B16 — the
# nightly cron used to invoke ``ansible-playbook`` from the VPS, but that
# tool isn't installed there. The cron now ``docker compose exec``s this
# script directly, mirroring the suspension-sweep cron pattern).
COPY deploy/ansible/scripts/backup-keys.sh /usr/local/bin/compadre-keys-backup
RUN chmod +x /usr/local/bin/compadre-keys-backup

# Copy entrypoint script
COPY scripts/entrypoint.sh /app/scripts/entrypoint.sh
RUN chmod +x /app/scripts/entrypoint.sh

# Create non-root user and volume mount points.
# /var/lib/compadre is the compadre-registry named-volume mountpoint. Pre-creating
# it appuser-owned ensures Docker's "copy mountpoint ownership into empty named
# volume on first mount" behaviour gives the volume the right owner — without
# this, the volume comes up root:root 0755 and the non-root appuser cannot
# create registry.db (sqlite3.OperationalError: unable to open database file).
# /app/models is where faster-whisper downloads large-v3-turbo via HuggingFace.
RUN useradd --create-home --shell /bin/bash appuser && \
    mkdir -p /app/models /var/lib/compadre && \
    chown -R appuser:appuser /app /var/lib/compadre

ENV PATH="/app/.venv/bin:$PATH"
ENV HF_HOME="/app/models"

USER appuser

EXPOSE 8000

ENTRYPOINT ["/app/scripts/entrypoint.sh"]
