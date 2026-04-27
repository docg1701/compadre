"""FR49 non-regression — Caddy body-framing + oversize policy (Story 4.5d).

Policy mirrors AWS API Gateway / Cloudflare / nginx ``client_max_body_size``:
1. Any ``Transfer-Encoding`` on ``@api`` → 411 (force Content-Length).
2. ``Content-Length`` non-canonical (non-numeric / >10 digits) → 400.
3. Valid ``Content-Length > 1 MiB`` → 413.
4. Write method without framing headers → 411.
5. Otherwise → proxy to upstream.
"""

from __future__ import annotations

import re
import socket
import ssl
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_ONE_MB: int = 1024 * 1024
_TWO_MB: int = 2 * _ONE_MB
_SUB_BOUNDARY: int = _ONE_MB - 1024
_STUB_SIGNATURE: bytes = b"non-regression-stub"
_API_PATHS: tuple[str, ...] = ("/v1/audio/transcriptions", "/health")
_OCTET_STREAM: dict[str, str] = {"Content-Type": "application/octet-stream"}

_REPO_ROOT: Path = Path(__file__).resolve().parents[2]
_CADDYFILE: Path = _REPO_ROOT / "deploy" / "caddy" / "Caddyfile"
_CADDYFILE_NO_LANDING: Path = _REPO_ROOT / "deploy" / "caddy" / "Caddyfile-no-landing"

_EXPECTED_MATCHERS: tuple[str, ...] = (
    "@api_has_te",
    "@api_malformed_cl",
    "@api_oversized_body",
    "@api_missing_cl",
)


def _chunked_bytes(total: int, chunk_size: int = 8192) -> Iterator[bytes]:
    """Yield ``total`` bytes as chunks; httpx switches to chunked TE."""
    remaining = total
    while remaining > 0:
        n = min(chunk_size, remaining)
        yield b"x" * n
        remaining -= n


def _send_raw_https(raw_request: bytes) -> tuple[int, bytes]:
    """Send a handcrafted HTTP/1.1 request over TLS to 127.0.0.1:443.

    Used when httpx would normalise the wire shape (forced
    ``Content-Length: 0`` on empty bodies, recomputed CL, etc.)."""
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    with (
        socket.create_connection(("127.0.0.1", 443), timeout=10.0) as sock,
        ctx.wrap_socket(sock, server_hostname="localhost") as ssock,
    ):
        ssock.settimeout(10.0)
        ssock.sendall(raw_request)
        chunks: list[bytes] = []
        while True:
            try:
                chunk = ssock.recv(4096)
            except (TimeoutError, OSError):
                break
            if not chunk:
                break
            chunks.append(chunk)
    raw = b"".join(chunks)
    if not raw:
        raise AssertionError(f"Raw HTTPS probe returned 0 bytes: {raw_request[:200]!r}")
    head, _, body = raw.partition(b"\r\n\r\n")
    parts = head.split(b"\r\n", 1)[0].split(b" ", 2)
    if len(parts) < 2 or not parts[1].isdigit():
        raise AssertionError(f"Unparseable status line: {head[:200]!r}")
    return int(parts[1]), body


@pytest.mark.non_regression
@pytest.mark.parametrize("path", _API_PATHS)
def test_oversized_post_is_rejected_with_413(
    probe_client: httpx.Client, path: str
) -> None:
    """2 MiB POST with Content-Length → 413 from ``@api_oversized_body``,
    body does NOT reach upstream."""
    payload = b"x" * _TWO_MB
    resp = probe_client.post(path, content=payload, headers=_OCTET_STREAM)
    assert resp.status_code == 413, (
        f"FR49: oversized POST {path} expected 413, got {resp.status_code}"
    )
    assert _STUB_SIGNATURE not in resp.content


@pytest.mark.non_regression
def test_sub_boundary_post_is_accepted_and_reaches_upstream(
    probe_client: httpx.Client,
) -> None:
    """A POST just under 1 MiB is accepted AND reaches upstream — proves
    the threshold sits at ~1 MiB rather than an arbitrarily low value."""
    payload = b"x" * _SUB_BOUNDARY
    resp = probe_client.post(
        "/v1/audio/transcriptions", content=payload, headers=_OCTET_STREAM
    )
    assert resp.status_code != 413
    assert _STUB_SIGNATURE in resp.content


@pytest.mark.non_regression
@pytest.mark.parametrize("te_value", ("chunked", "identity", "gzip", "deflate"))
def test_transfer_encoding_is_rejected_before_upstream(te_value: str) -> None:
    """Any ``Transfer-Encoding`` on ``@api`` → 411 from ``@api_has_te``, or
    501 from Go's net/http for unsupported TE values (RFC-7230 §3.3.1).

    Defends against Caddy 2.11.2's silent chunked-truncation bug
    (verified 2026-04-24): ``request_body max_size`` paired with
    ``reverse_proxy`` truncates oversized chunked uploads."""
    if te_value == "chunked":
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        with httpx.Client(
            base_url="https://127.0.0.1:443",
            verify=False,
            timeout=15.0,
            headers={"Host": "localhost"},
        ) as client:
            resp = client.post(
                "/v1/audio/transcriptions",
                content=_chunked_bytes(_TWO_MB),
                headers=_OCTET_STREAM,
            )
            status, body = resp.status_code, resp.content
        assert status == 411
    else:
        raw = (
            f"POST /v1/audio/transcriptions HTTP/1.1\r\n"
            f"Host: localhost\r\n"
            f"Content-Type: application/octet-stream\r\n"
            f"Transfer-Encoding: {te_value}\r\n"
            f"Connection: close\r\n\r\n"
        ).encode()
        status, body = _send_raw_https(raw)
        assert status in (411, 501), (
            f"TE={te_value!r}: expected 411 or 501, got {status}"
        )
    assert _STUB_SIGNATURE not in body


@pytest.mark.non_regression
@pytest.mark.parametrize("method", ("POST", "PUT", "PATCH"))
def test_write_without_body_framing_is_rejected_with_411(method: str) -> None:
    """POST/PUT/PATCH with neither CL nor TE → 411. Raw socket because
    httpx forces CL: 0 on empty bodies."""
    raw = (
        f"{method} /v1/audio/transcriptions HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    status, body = _send_raw_https(raw)
    assert status == 411
    assert _STUB_SIGNATURE not in body


@pytest.mark.non_regression
@pytest.mark.parametrize("cl_value", ("abc", "-1", "12abc", "99999999999"))
def test_malformed_content_length_returns_400(cl_value: str) -> None:
    """Non-canonical CL → 400 from ``@api_malformed_cl``. Closes the int()
    CEL-cast bypass: without ``^[0-9]{1,10}$`` regex pre-guard, the
    oversized matcher errors on non-numeric / >10-digit values and
    evaluates to false."""
    raw = (
        f"POST /v1/audio/transcriptions HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Content-Type: application/octet-stream\r\n"
        f"Content-Length: {cl_value}\r\n"
        f"Connection: close\r\n\r\n"
    ).encode()
    status, body = _send_raw_https(raw)
    assert status == 400, f"CL={cl_value!r}: expected 400, got {status}"
    assert _STUB_SIGNATURE not in body


@pytest.mark.non_regression
def test_oversized_post_to_non_api_path_is_not_413(
    probe_client: httpx.Client,
) -> None:
    """2 MiB POST to landing ``/`` must NOT return 413 — proves the policy is
    @api-scoped. A regression broadening to ``/*`` lands red here."""
    payload = b"x" * _TWO_MB
    resp = probe_client.post("/", content=payload, headers=_OCTET_STREAM)
    assert resp.status_code != 413
    assert resp.status_code < 500, f"got 5xx={resp.status_code}, Caddy crashed"


@pytest.mark.non_regression
def test_caddyfile_variants_share_the_same_body_framing_policy() -> None:
    """Static parity probe: ``Caddyfile`` and ``Caddyfile-no-landing`` must
    declare the same four ``@api_*`` matchers + paired handle blocks +
    same expression strings + same 1 MiB threshold."""
    with_landing = _CADDYFILE.read_text()
    no_landing = _CADDYFILE_NO_LANDING.read_text()

    for matcher in _EXPECTED_MATCHERS:
        for src, name in (
            (with_landing, "Caddyfile"),
            (no_landing, "Caddyfile-no-landing"),
        ):
            assert matcher in src, f"{matcher} missing from {name}"
            assert f"handle {matcher}" in src, f"handle {matcher} missing from {name}"

    expression_lines = [
        line.strip()
        for line in with_landing.splitlines()
        if line.strip().startswith("expression ")
    ]
    assert expression_lines, "no `expression` lines in Caddyfile"
    for expr in expression_lines:
        assert expr in no_landing, f"drift — {expr!r} not in no-landing"

    threshold_pattern = re.compile(
        r"int\(\{http\.request\.header\.content-length\}\) > (\d+)"
    )
    with_thresholds = set(threshold_pattern.findall(with_landing))
    no_thresholds = set(threshold_pattern.findall(no_landing))
    assert with_thresholds == no_thresholds == {"1048576"}, (
        f"oversize threshold drift: with={with_thresholds}, no={no_thresholds}"
    )
