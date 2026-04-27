#!/usr/bin/env bash
set -euo pipefail

exec uvicorn compadre.main:create_app \
    --factory \
    --host 0.0.0.0 \
    --port "${PORT:-8000}"
