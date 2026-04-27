# Non-Regression Suite

This package holds the CI non-regression contract — the mechanical checks
that prove controls the PRD marks as *must-not-regress* still hold on every
PR. Some checks run against a locally-spawned `docker` stack (the production
Caddy image with the production `deploy/caddy/Caddyfile`, hitting a stubbed
upstream instead of the real `tts-service` container); others exercise the
FastAPI app directly via an ASGI TestClient. The common contract is: they
run in the CI `non-regression` job and are excluded from the default
developer `pytest` invocation.

Ownership per sub-story (Epic 4, Story 4.5a-g):

| File                         | Sub-story | Controls covered                                            |
| ---------------------------- | --------- | ----------------------------------------------------------- |
| `test_security_headers.py`   | 4.5a      | HSTS, CSP, X-Frame-Options, X-Content-Type-Options, Referrer-Policy, Permissions-Policy, CSP `upgrade-insecure-requests` directive; Server / Via banner stripping |
| `test_docs_disabled.py`      | 4.5b      | FR47 — `/docs`, `/redoc`, `/openapi.json` 404 + project envelope + no OpenAPI content leak |
| `test_timing_safe_compare.py`| 4.5c      | FR48 — static AST scan forbids bare `==`/`!=` on `*hash*`/`*token*`/`*secret*` identifiers in `src/compadre/`, with `# scan-allow: <reason>` line-comment allowlist |
| `test_request_body_limit.py` | 4.5d      | FR49 — Caddy `@api` body-framing + oversize policy (any-TE/malformed-CL/missing-framing/>1MiB rejected; includes static Caddyfile-parity probe; see module docstring) |
| `test_rate_limit_wired.py`   | 4.5e      | FR50 — per-key rate-limit gate on `/v1/audio/speech`: `Depends(check_rate_limit)` present + `app.state.rate_limiter` initialised + 429 on `rate_limit_rpm+1`; negative assertion `/health` UN-rate-limited. ASGI TestClient (no docker). See module docstring for the 2.0.0 re-interpretation of FR50's slowapi-era wording. |
| `test_no_raw_httpexception.py` | 4.5f    | FR51 — static AST scan forbids ``raise HTTPException(...)`` AND ``from {fastapi, fastapi.exceptions, starlette.exceptions} import HTTPException`` in ``src/compadre/``, with ``# scan-allow: <reason>`` line-comment allowlist (same contract as 4.5c). Remediation hint names the ``ComadreError`` hierarchy at ``src/compadre/exceptions.py``. |
| `test_sql_static_bandit.py`  | 4.5g      | SQL-injection prevention — subprocess-runs ``python -m bandit -c <sentinel> -r src/compadre/keys/ -t B608 -ll -f json -q``, asserts zero B608 (``hardcoded_sql_expressions``) findings at MEDIUM-or-higher severity (``-ll``; bandit's ``-l`` means LOW-or-higher). Controlled-input column/identifier splicing sites use bandit's native ``# nosec B608  # <reviewer-enforced reason>`` two-comment escape hatch (orthogonal to 4.5c/4.5f's ``# scan-allow:`` token — see "Scan allowlist convention"). |

## Opt-in contract

The whole package is gated behind the `non_regression` pytest marker.
Running `uv run pytest` on a developer laptop (no flag) does **not** collect
or run these tests. Some tests need `docker compose` and bind privileged
ports; others use an ASGI TestClient and run in-process. The common
contract is: they run in the CI `non-regression` job and are excluded from
the default developer `pytest` invocation.

To run the suite locally:

```bash
uv run pytest -m non_regression tests/test_non_regression/ -v
```

In CI the `non-regression` job in `.github/workflows/ci.yml` runs exactly
this command.

## Fixture API

Two fixtures live in `tests/test_non_regression/conftest.py`. Every new test
module below this directory composes on them; no other fixture surface is
expected.

### `compose_stack` (session-scoped)

Spawns the stack via `subprocess` + `docker` CLI and yields the HTTPS base
URL (`https://localhost`). Teardown runs regardless of test outcome
(success, failure, `pytest.fail`, `KeyboardInterrupt`) via the fixture's
`finally` block; the CI workflow adds a belt-and-braces `if: always()` step
that greps containers by the `compadre-non-regression` label.

The fixture skips the module (not fails it) if `docker` is missing — so a
contributor running `pytest -m non_regression` without docker gets a clear
skip reason instead of a subprocess traceback.

### `probe_client` (function-scoped)

An `httpx.Client` with:

- `base_url=compose_stack` (HTTPS, `localhost`)
- `verify=False` (Caddy generates a self-signed cert for `localhost`)
- `follow_redirects=False` (the HTTP->HTTPS 308 is a first-class probe
  target, not a transparent redirect to hide)
- `timeout=5.0`

Function-scoped so each test gets a fresh connection pool — no cookie or
HTTP/2-stream bleed between probes.

## Stubbing strategy

**Strategy A — caddy-only + nginx stub on a user-defined Docker network.**

We start two containers labeled `compadre-non-regression`:

1. `nginx:alpine` aliased as `tts-service` on a user-defined network,
   listening on port 8000 with a two-line config that returns 200 on any
   path.
2. `caddy:2-alpine` bound to `127.0.0.1:80` / `127.0.0.1:443`, with the
   production `deploy/caddy/Caddyfile` bind-mounted read-only and
   `deploy/landing` bind-mounted at `/srv/www:ro`.

Caddy's `dynamic a { name tts-service port 8000 }` resolves the nginx stub
via Docker's embedded DNS (the user-defined network resolves container
names / network aliases automatically).

### Why NOT the real `tts-service`

- First-boot model download is ~1.5 GB (Kokoro + Qwen3 + tokenizer) and
  takes multiple minutes on a GitHub-hosted runner.
- Story 4.5a tests Caddy's header surface, not the TTS pipeline. Caddy
  applies its `header` block to *whatever* the upstream returns (200, 500,
  502); a stub returning 200 on every path is sufficient for header-presence
  assertions.
- `GET /not-a-real-path` and `POST /` don't even hit the upstream — they're
  served by Caddy's `file_server` handler directly.

A full-stack CI smoke (real `tts-service`) would catch regressions the
stub misses (engine warm-up, signal handling, volume mounts) but is
deliberately out of scope here — add it as a separate workflow file
when a real regression slips past the stub.

### Why Strategy A over a `compose.ci.yaml` override (Strategy B)

Strategy A is ~50 lines of fixture Python plus two standalone `docker run`
invocations. Strategy B would require a parallel compose override file that
drifts as `compose.yaml` evolves (new env var, new volume, new healthcheck
tweak). Strategy A has zero cross-file drift risk.

If Caddy's `dynamic a` DNS resolver ever misbehaves against network aliases
in a future Docker release, falling back to Strategy B is mechanical: drop
`conftest.py`'s `subprocess.run(["docker", "run", ...])` calls and replace
with `subprocess.run(["docker", "compose", "-f", "compose.yaml", "-f",
"compose.ci.yaml", "up", "-d"])`.

## Known-leak exclusions (important)

Three response classes bypass the site-level `header { ... }` middleware in
the current Caddyfile. All three are architectural (not bugs) and all three
are `pytest.skip`ped from security-header-presence AND banner-strip
assertions so a future contributor can un-skip with confidence the day the
underlying behavior changes.

- **HTTP 404 and 405 from `file_server`.** Caddy's `file_server` directive
  writes error responses (path-not-found, method-not-allowed) directly via
  its internal error handler, *before* the site-block `header` directive
  can inject the full security-header set or strip `Server:` / `Via:`. The
  result: these two status codes carry neither the 6 required headers nor
  the `-Server` strip. A future hardening story can close this gap via a
  Caddyfile `handle_errors` block that re-applies the full header set to
  error responses — empirically the approach works in Caddy 2-alpine.
  Out of Story 4.5a's "pure test infra + CI wiring" scope; see the story's
  deferred-work section.
- **HTTP 308 automatic HTTP->HTTPS redirect.** Issued from Caddy's global
  `servers` block *before* the site block runs, so neither the site-level
  `-Server` strip nor the site-level security-header directives apply.
  Unlike the 404/405 case, the 308 exclusion is *truly* structural — no
  `handle_errors` pattern can reach responses issued by the global block.
  A dedicated test asserts the 308 exists and explicitly documents the
  double-exclusion inline.

**Do not "fix" the exclusions by deleting the skips and asserting.** The
exclusions encode Caddy's current middleware ordering, not bugs in the
suite. When the production Caddyfile ships a `handle_errors` block that
closes the 404/405 gap, re-run the suite without the skip and verify all
probes pass — only THEN remove the skip.

## Extension contract for stories 4.5b-4.5g

Each downstream sub-story SHOULD:

1. Add exactly one new `test_<check_name>.py` file to this directory.
2. Import fixtures from this module's `conftest.py` (no new fixtures needed
   for the common case).
3. Mark every test `@pytest.mark.non_regression` so it runs in the CI job
   and is excluded from the default developer `pytest` invocation.
4. Keep assertion messages self-explanatory: `(a) which control regressed,
   (b) which HTTP method + path, (c) observed status code, (d) a diagnostic
   tail` — so the PR-build red message names the exact regression.

Sub-stories SHOULD NOT:

- Modify `.github/workflows/ci.yml` — the existing `non-regression` job
  already discovers any test in this directory that carries the marker.
- Add a new pytest marker — the one marker `non_regression` is sufficient.
- Place tests outside `tests/test_non_regression/` — AC15 of Story 4.5a is
  a grep invariant that catches drift: `rg pytest.mark.non_regression
  tests/ | grep -v test_non_regression/` MUST return empty.
- Exercise `Caddyfile-no-landing` unless the two files diverge. Their
  `header` blocks are currently byte-identical (primary lines 8-17,
  no-landing lines 8-17); covering the primary covers the no-landing
  variant transitively. A pre-commit hook asserting byte-identity is
  candidate deferred-work.

### Harness choice

Sub-stories pick ONE of two harness shapes, per the nature of the check:

- **Docker stack** (`compose_stack` + `probe_client` from this package's
  `conftest.py`). Use when the check crosses the Caddy↔FastAPI boundary —
  e.g., FR46 security headers (4.5a), FR49 Caddy request-body limit (4.5d).
- **ASGI TestClient** (per-module fixture wrapping `create_app()`). Use
  when the check is app-internal — e.g., FR47 docs-disabled (4.5b), FR50
  rate-limit middleware registration (4.5e) — or when it's a static scan
  that needs no HTTP harness at all (4.5c, 4.5f, 4.5g).

Rule of thumb: pick TestClient when the check is app-internal (FR47, FR50);
pick docker when the check crosses the Caddy↔FastAPI boundary (FR46, FR49).
Static-scan checks skip both harnesses.

### Scan allowlist convention

Static-scan sub-stories (4.5c, 4.5f, 4.5g) share one escape hatch: a
trailing `# scan-allow: <reason>` comment on the offending source line
suppresses the scan for that line only. The reason is free-form but
reviewer-enforced — an empty or one-word reason is a review-blocker in
practice. Each scan owns its own reason vocabulary; a reason accepted by
4.5c's timing-safe scan is not automatically accepted by 4.5f's
no-raw-`HTTPException` scan (they exist on different lines anyway).

Fixture files driving each scan's self-tests live under
`tests/test_non_regression/fixtures/<check_name>/` — e.g., 4.5c uses
`fixtures/timing_safe_compare/`. This keeps fixture surface partitioned
per-sub-story so a future scan can land without collision.

### Bandit allowlist convention (4.5g)

Sub-story 4.5g delegates its scan to `bandit`, which ships its own native
escape hatch: a trailing `# nosec <test-ids>` comment on the offending
source line. 4.5g's gate uses the **two-comment form**
`# nosec B608  # <reason>` specifically:

- The explicit test-id `B608` narrows the suppression to the SQL plugin
  only (a bare `# nosec` would silence all bandit findings on that line,
  which is too broad).
- The reason after the **second** `#` is invisible to bandit's tolerant
  nosec lexer — bandit reads test-ids only until the next `#` boundary,
  so reason text never trips the `[manager] WARNING Test in comment:
  <word> is not a test name or id, ignoring` stderr noise that the
  single-comment colon form (`# nosec B608: <reason>`) emits once per
  word.

The `# nosec` vocabulary is complementary to the `# scan-allow:`
vocabulary 4.5c/4.5f share; the two tokens are orthogonal and a line
could in principle carry both if it tripped multiple scans
simultaneously. Same reviewer-enforced reason-vocabulary rules apply:
the reason must name the controlled-input contract (which collection is
spliced, why it's safe, how values bind).
