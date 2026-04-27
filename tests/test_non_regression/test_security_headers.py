"""FR46 + Server/Via banner-strip non-regression assertions (Story 4.5a).

Lean by design (test-suite review 2026-04-25): assertion-message prose
trimmed; one consolidated runtime probe per (method, path) replaces three
separate parametrised tests over the same probes.
"""

from __future__ import annotations

import re
from pathlib import Path

import httpx
import pytest

REQUIRED_HEADERS: tuple[str, ...] = (
    "Strict-Transport-Security",
    "Content-Security-Policy",
    "X-Frame-Options",
    "X-Content-Type-Options",
    "Referrer-Policy",
    "Permissions-Policy",
)

# (method, path, expected_status_or_None). None = "shape-agnostic" (probe
# exists to exercise Caddy's header surface, not pin upstream's response).
PROBES: tuple[tuple[str, str, int | None], ...] = (
    ("GET", "/", 200),
    ("POST", "/v1/audio/transcriptions", None),
    ("GET", "/health", None),
    ("GET", "/this-path-does-not-exist", 404),
    ("POST", "/", 405),
)

# F67 + F68 (2026-04-25 hardening): Caddy's site-level `handle_errors` block
# re-applies the security headers + Server/Via strip on every Caddy-emitted
# error response (404 / 405 / 411 / 413 / etc.). The HTTP->HTTPS 308 remains
# excluded — issued from the GLOBAL automatic-HTTPS block, not the site block.


@pytest.mark.non_regression
@pytest.mark.parametrize(("method", "path", "expected_status"), PROBES)
def test_security_headers_and_banner_strip(
    probe_client: httpx.Client,
    method: str,
    path: str,
    expected_status: int | None,
) -> None:
    """One probe per (method, path) asserts: status (when pinned), all 6
    required security headers present, CSP carries upgrade-insecure-requests,
    Server + Via banners stripped."""
    resp = probe_client.request(method, path)
    if expected_status is not None:
        assert resp.status_code == expected_status, (
            f"{method} {path} status {resp.status_code} != {expected_status}"
        )
    for header in REQUIRED_HEADERS:
        assert header in resp.headers, (
            f"Missing {header!r} on {method} {path} ({resp.status_code})"
        )
    csp = resp.headers.get("Content-Security-Policy", "")
    assert "upgrade-insecure-requests" in csp, (
        f"CSP missing upgrade-insecure-requests on {method} {path}: {csp!r}"
    )
    assert "Server" not in resp.headers, (
        f"Server leaked on {method} {path}: {resp.headers.get('Server')!r}"
    )
    assert "Via" not in resp.headers, (
        f"Via leaked on {method} {path}: {resp.headers.get('Via')!r}"
    )


# ---------------------------------------------------------------------------
# F48 — static parity assertion on the site-level header { ... } block
# across both production Caddyfiles.
# ---------------------------------------------------------------------------


_HEADER_BLOCK_RE = re.compile(r"\bheader\s*\{(?P<body>[^}]*)\}", re.DOTALL)


@pytest.mark.non_regression
@pytest.mark.parametrize("caddyfile_name", ("Caddyfile", "Caddyfile-no-landing"))
def test_caddyfile_security_headers_block_present(caddyfile_name: str) -> None:
    """F48 — both production Caddyfiles MUST declare every required security
    header + Server/Via strips. Byte-identity isn't the contract (the
    no-landing variant has a simpler CSP without font-src/img-src/script-src);
    presence + name catches the most common drift class at PR time."""
    repo_root = Path(__file__).resolve().parents[2]
    caddyfile = repo_root / "deploy" / "caddy" / caddyfile_name
    text = caddyfile.read_text(encoding="utf-8")
    match = _HEADER_BLOCK_RE.search(text)
    if match is None:
        pytest.fail(f"No `header {{ ... }}` block found in {caddyfile}")
    block = match.group("body").strip()

    for header_name in REQUIRED_HEADERS:
        assert header_name in block, (
            f"{caddyfile_name}: header block missing {header_name!r}"
        )
    assert "-Server" in block, f"{caddyfile_name}: header block missing `-Server`"
    assert "-Via" in block, f"{caddyfile_name}: header block missing `-Via`"


@pytest.mark.non_regression
def test_http_to_https_redirect_is_308_and_excluded_from_banner_strip(
    compose_stack: str,
) -> None:
    """The 308 is issued from Caddy's automatic-HTTPS GLOBAL block, not the
    site block, so it cannot carry the site's `header` directives. This test
    documents the exception so a future reader does not delete it as
    suspicious. See feedback_caddy_server_header.md."""
    assert compose_stack == "https://localhost"
    resp = httpx.get(
        "http://localhost/", verify=False, follow_redirects=False, timeout=5.0
    )
    assert resp.status_code == 308, f"http://localhost/ status {resp.status_code}"
