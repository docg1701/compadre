"""Shared fixtures for the CI non-regression suite.

Boots the production Caddy image with the production Caddyfile against a
stubbed nginx upstream so assertions exercise the real reverse-proxy
header surface without the faster-whisper model-download cost (~1.5 GB).
"""

from __future__ import annotations

import shutil
import ssl
import subprocess
import time
import warnings
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

warnings.filterwarnings("ignore", message=".*[Uu]nverified HTTPS.*")

_LABEL = "compadre-non-regression"
_NETWORK = "compadre-non-regression-net"
_CADDY_CONTAINER = "compadre-nr-caddy"
_UPSTREAM_CONTAINER = "compadre-nr-upstream"
_UPSTREAM_HOSTNAME = "compadre-stt"
_BASE_URL = "https://localhost"

# Story 4.5d: stub's body-size ceiling raised to 100 MB so any 4xx the suite
# observes originates UNAMBIGUOUSLY from Caddy, never from nginx's default.
_UPSTREAM_NGINX_CONF = """\
events {}
http {
    access_log /dev/null;
    client_max_body_size 100M;
    server {
        listen 8000 default_server;
        location / {
            add_header Content-Type text/plain;
            return 200 "non-regression-stub\\n";
        }
    }
}
"""

_READY_EXCEPTIONS: tuple[type[BaseException], ...] = (
    httpx.RequestError,
    ssl.SSLError,
    OSError,
)


def _docker(args: list[str]) -> subprocess.CompletedProcess[str]:
    """Run ``docker``, surfacing stderr via pytest.fail on non-zero exit."""
    try:
        return subprocess.run(
            ["docker", *args], check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as exc:
        detail = (exc.stderr or exc.stdout or "").strip() or "(no output)"
        pytest.fail(f"docker {' '.join(args)} failed (exit {exc.returncode}): {detail}")


def _prepull_images() -> None:
    """Pre-pull so a slow pull surfaces as a pull error, not a confusing
    'Caddy did not come up' timeout under DockerHub throttling."""
    for image in ("caddy:2-alpine", "nginx:alpine"):
        subprocess.run(["docker", "pull", image], capture_output=True, check=False)


def _tear_down_stack() -> None:
    probe = subprocess.run(
        ["docker", "ps", "-aq", "--filter", f"label={_LABEL}"],
        capture_output=True,
        text=True,
        check=False,
    )
    ids = [line for line in probe.stdout.split() if line]
    if ids:
        subprocess.run(["docker", "rm", "-f", *ids], capture_output=True, check=False)
    subprocess.run(
        ["docker", "network", "rm", _NETWORK], capture_output=True, check=False
    )


def _verify_upstream_reachable() -> None:
    """Probe ``GET /health`` (matches @api, proxied to stub) so a stub that
    failed to start surfaces as a fixture error rather than a confusing
    'stub signature missing' assertion. ``/`` would not catch this — Caddy
    serves it from file_server without touching reverse_proxy."""
    try:
        resp = httpx.get(
            f"{_BASE_URL}/health", verify=False, follow_redirects=False, timeout=2.0
        )
    except _READY_EXCEPTIONS as exc:
        raise RuntimeError(
            f"Stub unreachable through Caddy: {exc!r}. "
            f"Check `docker logs {_UPSTREAM_CONTAINER}`."
        ) from exc
    if resp.status_code >= 500:
        raise RuntimeError(f"Stub returned {resp.status_code} on /health")
    if b"non-regression-stub" not in resp.content:
        raise RuntimeError(
            f"GET /health status={resp.status_code} but stub signature missing — "
            f"Caddy may be answering /health itself. Body: {resp.content[:200]!r}"
        )


def _wait_for_caddy(timeout_seconds: float = 30.0) -> None:
    """Poll HTTP+HTTPS until both return non-5xx, then verify upstream stub.

    Earlier impl returned on first HTTPS success regardless of status,
    which fired before Caddy's self-signed cert was provisioned or port 80
    was bound — both latent flakes."""
    deadline = time.monotonic() + timeout_seconds
    last_error: object = None
    while time.monotonic() < deadline:
        try:
            https_resp = httpx.get(
                f"{_BASE_URL}/", verify=False, follow_redirects=False, timeout=2.0
            )
            http_resp = httpx.get(
                "http://localhost/", follow_redirects=False, timeout=2.0
            )
            if https_resp.status_code < 500 and http_resp.status_code < 500:
                _verify_upstream_reachable()
                return
            last_error = (
                f"HTTPS {https_resp.status_code} / HTTP {http_resp.status_code}"
            )
        except _READY_EXCEPTIONS as exc:
            last_error = exc
        time.sleep(0.5)
    raise RuntimeError(
        f"Caddy stack did not come up in {timeout_seconds}s: {last_error!r}"
    )


@pytest.fixture(scope="session")
def compose_stack(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    """Yield the HTTPS base URL of a locally-spawned Caddy + nginx stack.

    Skips (not fails) when docker is unavailable so a developer running
    ``pytest -m non_regression`` without docker gets a clear reason."""
    if shutil.which("docker") is None:
        pytest.skip("docker CLI not on PATH")
    probe = subprocess.run(
        ["docker", "version", "--format", "{{.Server.Version}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        pytest.skip(
            f"docker daemon unavailable: {(probe.stderr or probe.stdout).strip()}"
        )

    _prepull_images()

    repo_root = Path(__file__).resolve().parents[2]
    caddyfile = repo_root / "deploy" / "caddy" / "Caddyfile"
    landing_dir = repo_root / "deploy" / "landing"
    landing_index = landing_dir / "index.html"
    if not caddyfile.is_file():
        pytest.fail(f"Caddyfile missing at {caddyfile}")
    if not landing_dir.is_dir():
        pytest.fail(f"Landing dir missing at {landing_dir}")
    # Without index.html the root probe returns 404 and security-headers
    # assertions silently exercise the error path.
    if not landing_index.is_file():
        pytest.fail(f"Landing index missing at {landing_index}")

    tmp_dir = tmp_path_factory.mktemp("non-regression")
    nginx_conf = tmp_dir / "nginx.conf"
    nginx_conf.write_text(_UPSTREAM_NGINX_CONF)

    _tear_down_stack()

    try:
        _docker(["network", "create", "--label", f"{_LABEL}=true", _NETWORK])
        _docker(
            [
                "run",
                "-d",
                "--name",
                _UPSTREAM_CONTAINER,
                "--hostname",
                _UPSTREAM_HOSTNAME,
                "--network",
                _NETWORK,
                "--network-alias",
                _UPSTREAM_HOSTNAME,
                "--label",
                _LABEL,
                "-v",
                f"{nginx_conf}:/etc/nginx/nginx.conf:ro",
                "nginx:alpine",
            ]
        )
        _docker(
            [
                "run",
                "-d",
                "--name",
                _CADDY_CONTAINER,
                "--network",
                _NETWORK,
                "--label",
                _LABEL,
                "-p",
                "127.0.0.1:80:80",
                "-p",
                "127.0.0.1:443:443",
                "-v",
                f"{caddyfile}:/etc/caddy/Caddyfile:ro",
                "-v",
                f"{landing_dir}:/srv/www:ro",
                "-e",
                "DOMAIN=localhost",
                "caddy:2-alpine",
            ]
        )
        _wait_for_caddy()
        yield _BASE_URL
    finally:
        _tear_down_stack()


@pytest.fixture()
def probe_client(compose_stack: str) -> Iterator[httpx.Client]:
    """HTTPS client for the locally-spawned Caddy stack. ``verify=False``
    (self-signed) and ``follow_redirects=False`` (the 308 is a probe target)."""
    with httpx.Client(
        base_url=compose_stack, verify=False, follow_redirects=False, timeout=5.0
    ) as client:
        yield client
