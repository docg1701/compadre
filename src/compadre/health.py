"""Health response assembler for Compadre."""

from __future__ import annotations

import resource
import sys
import time
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from compadre import __version__
from compadre.logging import get_logger

if TYPE_CHECKING:
    from compadre.stt import WhisperEngine

logger = get_logger("health")


class HealthStatus(StrEnum):
    """Service health status values."""

    healthy = "healthy"
    warming_up = "warming_up"
    degraded = "degraded"


def _get_memory_mb() -> int:
    """Return peak process RSS in megabytes (ru_maxrss).

    Linux reports ``ru_maxrss`` in kilobytes; macOS / BSD report it in bytes.
    Normalize to megabytes regardless of platform.
    """
    usage = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    if sys.platform == "darwin":
        # macOS / BSD: bytes → MB
        return usage // (1024 * 1024)
    # Linux: KB → MB
    return usage // 1024


def _get_uptime_seconds(start_time: float) -> int:
    """Return seconds since application startup."""
    return int(time.monotonic() - start_time)


def build_minimal_health_response(
    *,
    engine: WhisperEngine | None,
    ready: bool,
) -> tuple[dict[str, Any], int]:
    """Build minimal health response for unauthenticated callers.

    Returns only status and model list — no engine internals,
    memory, uptime, version, or queue depth.
    """
    if ready:
        status = HealthStatus.healthy
        status_code = 200
    else:
        status = HealthStatus.warming_up
        status_code = 503

    body: dict[str, Any] = {
        "status": str(status),
        "models": ["large-v3-turbo"],
    }
    return body, status_code


def build_health_response(
    *,
    engine: WhisperEngine | None,
    ready: bool,
    start_time: float,
    queue_depth: int = 0,
) -> tuple[dict[str, Any], int]:
    """Build health response dict and HTTP status code.

    Returns (response_body, status_code) tuple.
    Field order is contractual — dict insertion order is preserved in Python 3.7+.
    """
    if ready:
        status = HealthStatus.healthy
        status_code = 200
    else:
        status = HealthStatus.warming_up
        status_code = 503

    loaded = engine.is_loaded if engine is not None else False

    body: dict[str, Any] = {
        "status": str(status),
        "models": ["large-v3-turbo"],
        "engines": {
            "whisper": {
                "loaded": loaded,
                "model": "large-v3-turbo",
            },
        },
        "queue_depth": queue_depth,
        "uptime_seconds": _get_uptime_seconds(start_time),
        "memory_mb": _get_memory_mb(),
        "version": __version__,
    }

    return body, status_code
