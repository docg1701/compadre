# CI Runbook

**Last updated:** 2026-04-27

## Overview

The Compadre CI pipeline runs on every push and pull request to `master`/`main`. It consists of 7 parallel jobs that provide fast, granular pass/fail signal. All jobs must pass before merge.

## Job Reference

| Job | Command | Needs Docker | Timeout |
|-----|---------|-------------|---------|
| `lint` | `ruff check` + `ruff format --check` + `pre-commit run --all-files` | No | 5 min |
| `type-check` | `mypy src/` | No | 5 min |
| `test-fast` | `pytest -m "not slow and not non_regression"` | No | 10 min |
| `test-non-regression` | `pytest -m non_regression tests/test_non_regression/` | Yes | 15 min |
| `ansible-lint` | `ansible-lint` + `yamllint` | No | 5 min |
| `docker-build` | `docker build` (push: false) | Yes | 10 min |
| `coverage` | `pytest` with `coverage report --fail-under=60` | No | 10 min |

### Job Details

#### lint

Runs three checks sequentially:

- `ruff check .` — lint rules (E, F, I, UP, B, SIM, N, C4, PTH, RUF)

- `ruff format --check .` — formatting consistency (line-length 88)

- `pre-commit run --all-files` — hadolint, yamllint, shellcheck, markdownlint

Common failures:

- Line too long (>88 chars) → break the string across lines

- Ambiguous unicode character (RUF001) → replace with ASCII equivalent

- hadolint `DL3008` → pin apt package versions in Dockerfile

#### type-check

Runs `mypy src/` in strict mode. Failures typically mean a type annotation is missing or a third-party package lacks stubs.

Common failures:

- `Missing type stubs for package X` → add to `[[tool.mypy.overrides]]` with `ignore_missing_imports = true`

- `Argument has incompatible type` → fix the annotation or the call site

#### test-fast

Runs all unit and integration tests that do NOT require the real Whisper model or Docker containers. Uses `-m "not slow and not non_regression"` to exclude:

- Model-dependent tests (marker `slow` — currently unused, placeholder)

- Non-regression tests (marker `non_regression` — Docker/security scans)

The `ensure_whisper_model` function is monkeypatched in `conftest.py` so the model is never downloaded during these tests.

#### test-non-regression

Runs the CI non-regression suite. These tests enforce architecture contracts:

- Security header assertions (Caddy reverse proxy surface)

- Request body limits

- Rate limiter wiring

- Static scans: no raw HTTPException, no raw os.environ, single-Fernet-decrypt, timing-safe compare, SQL injection patterns

- Grep invariant: all `@pytest.mark.non_regression` tests live in `tests/test_non_regression/`

Pre-pulls `caddy:2-alpine` and `nginx:alpine` Docker images to avoid DockerHub throttling during the test run.

Common failures:

- Docker daemon not available → ensure GitHub Actions runner has Docker

- DockerHub rate limit → the cache step should mitigate; retry after 6 hours if persistent

- Non-regression test outside `test_non_regression/` → move the test or remove the marker

#### ansible-lint

Lints Ansible playbooks in `deploy/ansible/playbooks/` with `ansible-lint` and validates YAML syntax with `yamllint`. No collections are installed (lint-only).

Common failures:

- `fqcn-builtins` → use FQCN (e.g., `ansible.builtin.file`)

- `name` missing → add a `name:` to every task

- `yamllint` line-length → break long lines or use `>` folding

#### docker-build

Verifies the Docker image compiles using `docker/build-push-action@v6` with `push: false`. Uses GitHub Actions cache (`type=gha`) for layer caching.

Common failures:

- `COPY` source missing → check path relative to build context

- `apt-get install` fails → pin package versions (hadolint DL3008)

#### coverage

Runs the same test suite as `test-fast` with `coverage.py` and enforces a 60% threshold. Depends on `test-fast` (runs after, not blocking). Uploads `.coverage` as a build artifact.

Common failures:

- Coverage below 60% → add tests for untested code paths

- Coverage run fails → same debugging as test-fast

## Reproducing Locally

Run the bundled CI reproduction script:

```bash
bash scripts/ci-local.sh          # all steps (lint, type-check, test-fast)
bash scripts/ci-local.sh lint     # just lint step
bash scripts/ci-local.sh test-fast # just fast tests
```

Requirements:

- `uv` (Python package manager)

- `ffmpeg` (audio decoding — install via apt/brew)

- `docker` (optional, needed for `docker-build` and `test-non-regression` steps)

## Marker Contract

| Marker | Purpose | Test location | CI job |
|--------|---------|--------------|--------|
| `slow` | Real model inference | (unused, placeholder) | `test-slow` (future) |
| `non_regression` | Architecture contract enforcement | `tests/test_non_regression/` only | `test-non-regression` |

The `pyproject.toml` `addopts` defaults to `-m 'not non_regression'` so developer `pytest` never triggers Docker downloads or slow scans. CI jobs explicitly pass the marker they need.

### Grep Invariant

The following grep must return empty — non-regression tests live ONLY in `tests/test_non_regression/`:

```bash
grep -rl "pytest.mark.non_regression" tests/ | grep -v "test_non_regression/"
```

This is enforced as a CI step in the `test-non-regression` job.

## Dependency Groups

| Group | Tools | Used by CI jobs |
|-------|-------|----------------|
| `lint` | ruff, pre-commit | `lint` |
| `test` | pytest, httpx, coverage | `test-fast`, `test-non-regression`, `coverage` |
| `type-check` | mypy, types-pyyaml | `type-check` |
| `ci` | ansible-lint, yamllint | `ansible-lint` |
| `security` | bandit | (manual) |

Install all groups: `uv sync --group lint --group test --group type-check --group ci`

## Troubleshooting

### uv cache poisoning

If `astral-sh/setup-uv@v5` cache seems stale, delete the cache from the GitHub Actions UI (repo → Actions → Caches) and re-run.

### DockerHub rate limiting

GitHub Actions runners share IPs and can hit DockerHub pull limits. The `test-non-regression` job pre-pulls images which are cached by GitHub's Docker layer cache. If rate-limited, wait 6 hours or use an authenticated DockerHub account via `DOCKER_HUB_TOKEN` secret.

### Slow CI runs

First run after a `uv.lock` change will be slow (no uv cache). Subsequent runs benefit from `actions/cache`. Docker build caches layers via `type=gha`.

### Non-regression test failure

These tests enforce architecture contracts. Read the failure message carefully — it includes the violating file, line number, and remediation hint. Do not bypass without updating the architecture decision record.

## Adding a New Non-Regression Test

1. Create the test file in `tests/test_non_regression/`
2. Mark it `@pytest.mark.non_regression`
3. If using a static scan, register the label in `_scan_common.py::SCAN_LABELS`
4. If adding a justified exemption, use `# scan-allow[<label>]: <reason>` (min 3 char reason)
5. Verify with: `uv run pytest -m non_regression tests/test_non_regression/ -v`
