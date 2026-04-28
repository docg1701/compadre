"""Tests for ``/health`` (build_health_response + HTTP)."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

from compadre.health import build_health_response, build_minimal_health_response

_FULL_KEYS = [
    "status",
    "models",
    "engines",
    "queue_depth",
    "uptime_seconds",
    "memory_mb",
    "version",
]


@pytest.fixture()
def mock_engine() -> MagicMock:
    engine = MagicMock()
    engine.is_loaded = True
    return engine


def test_health_response_shapes_full_vs_minimal_and_503_when_warming(
    mock_engine: MagicMock,
) -> None:
    """Authenticated /health returns the full 7-field shape in fixed order
    (monitor-script contract); unauthenticated returns only ``status``
    (no model list or internals); ``ready=False`` flips minimal to
    503/warming_up."""
    full, full_status = build_health_response(
        engine=mock_engine, ready=True, start_time=time.monotonic()
    )
    assert full_status == 200
    assert list(full.keys()) == _FULL_KEYS
    assert full["status"] == "healthy"
    assert full["models"] == ["large-v3-turbo"]
    assert full["engines"]["whisper"]["loaded"] is True

    minimal, mini_status = build_minimal_health_response(engine=mock_engine, ready=True)
    assert mini_status == 200
    assert set(minimal.keys()) == {"status"}
    assert minimal["status"] == "healthy"

    warming, warming_status = build_minimal_health_response(
        engine=mock_engine, ready=False
    )
    assert warming_status == 503
    assert warming["status"] == "warming_up"


async def test_health_endpoint_minimal_unauth_full_auth(
    client: AsyncClient, auth_headers: dict[str, str]
) -> None:
    """End-to-end smoke: unauth → status-only shape; auth → full shape."""
    response = await client.get("/health")
    assert response.status_code == 200
    assert set(response.json().keys()) == {"status"}

    response = await client.get("/health", headers=auth_headers)
    assert response.status_code == 200
    assert set(response.json().keys()) == set(_FULL_KEYS)
