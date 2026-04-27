"""FR47 non-regression — /docs, /redoc, /openapi.json disabled (Story 4.5b).

The production Caddyfile's ``@api`` matcher routes only /v1/* and /health to
FastAPI; paths like /docs are served by Caddy's file_server and never reach
the app. This test exercises the app directly via ASGI TestClient so a PR
that re-enables ``docs_url`` / ``redoc_url`` / ``openapi_url`` in
``create_app()`` — or that ships an OpenAPI sub-app — fails CI even before
Caddy is consulted. The Caddy-level enforcement is a separate belt-and-braces
layer; see §Deferred-work for a full-stack variant.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from cryptography.fernet import Fernet
from httpx import ASGITransport, AsyncClient

from compadre.config import get_settings
from compadre.main import create_app

DISABLED_ENDPOINTS: tuple[str, ...] = ("/docs", "/redoc", "/openapi.json")

# Substrings that would indicate FastAPI's OpenAPI / Swagger UI / ReDoc
# machinery is leaking into the response body. Match is case-insensitive
# and done on the decoded text. Markers are chosen to be specific enough
# that the project's own envelope (a JSON body with message/type/hint
# strings) cannot accidentally trigger them — e.g., ``"openapi":`` is
# the JSON key in a served openapi.json spec, not a word that would
# appear inside a hint string.
_CONTENT_LEAK_MARKERS: tuple[str, ...] = (
    "swagger-ui-bundle",
    "swagger-ui.css",
    "redoc.standalone",
    "redoc-container",
    '"openapi":',
    "<!doctype html",
    "<html",
)


@pytest.fixture()
async def bare_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[AsyncClient]:
    """Minimal ASGI client over a bare ``create_app()`` instance.

    No seeded customer, no router mock, no rate-limiter — the /docs
    surface doesn't route through any of that. Only what the FastAPI
    ``startup`` path insists on: a valid Fernet key for the key-registry
    settings validator.
    """
    monkeypatch.setenv("KEY_REGISTRY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://test",
    ) as client:
        yield client


@pytest.mark.non_regression
@pytest.mark.parametrize("path", DISABLED_ENDPOINTS)
async def test_docs_endpoint_returns_404(bare_client: AsyncClient, path: str) -> None:
    resp = await bare_client.get(path)
    assert resp.status_code == 404, (
        f"{path} returned {resp.status_code}, expected 404. "
        f"If this is green because docs_url/redoc_url/openapi_url was re-enabled "
        f"in create_app(), close that gap before merging."
    )


@pytest.mark.non_regression
@pytest.mark.parametrize("path", DISABLED_ENDPOINTS)
async def test_docs_endpoint_uses_project_envelope(
    bare_client: AsyncClient, path: str
) -> None:
    resp = await bare_client.get(path)
    body = resp.json()
    assert "error" in body, (
        f"{path} 404 body missing 'error' key: {body!r}. "
        f"Did FastAPI's default 404 handler win the exception-handler race?"
    )
    error = body["error"]
    for key in ("message", "type", "hint"):
        assert key in error, f"{path} 404 envelope missing '{key}' field: {error!r}"
        assert isinstance(error[key], str), (
            f"{path} 404 envelope '{key}' is not a string: {error[key]!r}"
        )
        assert error[key], f"{path} 404 envelope '{key}' is an empty string: {error!r}"
    assert error["type"] == "not_found_error", (
        f"{path} 404 envelope type='{error['type']}', expected 'not_found_error'"
    )


@pytest.mark.non_regression
@pytest.mark.parametrize("path", DISABLED_ENDPOINTS)
async def test_docs_endpoint_body_has_no_openapi_content(
    bare_client: AsyncClient, path: str
) -> None:
    resp = await bare_client.get(path)
    body_lower = resp.text.lower()
    leaks = [marker for marker in _CONTENT_LEAK_MARKERS if marker in body_lower]
    assert not leaks, (
        f"{path} 404 body leaks OpenAPI / Swagger / ReDoc content "
        f"(markers found: {leaks}). Body: {resp.text[:500]!r}"
    )
