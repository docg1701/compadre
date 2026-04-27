# Implementation Plan — Plan 7: CI Pipeline Fix & Validation

## Goal
Restructure the CI pipeline from a single monolithic job into parallel quality gates (lint, type-check, test-fast, test-non-regression, ansible-lint, docker-build, coverage) so every PR and push to main/master receives fast, granular, and trustworthy pass/fail signal.

## Prerequisites
- Plan 3 (stable deploy) completed — the CI must reflect the deployed reality (Caddyfile, compose.yaml, Dockerfile, playbooks).

---

## Tasks

### Phase 1 — Diagnose current CI failures

1. **Reproduce the current CI workflow locally**
   - File: new `scripts/ci-local.sh`
   - Changes: Create a shell script that mirrors the exact steps of the current `ci.yml` job (install ffmpeg, uv sync, ruff, mypy, pytest) so failures can be reproduced on a developer laptop before pushing.
   - Acceptance: Running `bash scripts/ci-local.sh` produces the same pass/fail as the GitHub Actions CI job.

2. **Audit test suite for environment-sensitive failures**
   - File: run `uv run pytest --tb=long -q -m "not slow"` locally
   - Changes: Catalog every test failure with its root cause (missing env var, path assumption, import error, fixture timeout). Document in a `ci-diagnosis.md` scratch file.
   - Acceptance: A triaged list of all failures with proposed fixes assigned to each.

3. **Verify non-regression tests are never collected by default pytest AND by the current CI command**
   - File: `pyproject.toml` (inspect `addopts`), `.github/workflows/ci.yml`
   - Changes: Confirm `addopts = "-m 'not non_regression'"` excludes all 7 non-regression test files. **Also confirm that the CI command `pytest -m "not slow"` does NOT override the `addopts` exclusion.** Pytest's CLI `-m` flag **overrides** `addopts`, so `-m "not slow"` silently discards `-m 'not non_regression'`, causing non-regression tests to leak into the CI run (42 tests collected instead of 227). The fix: change the CI command to `pytest -m "not slow and not non_regression"`.
   - Acceptance: `uv run pytest --collect-only -q -m "not slow and not non_regression"` shows 227 tests. `grep` of CI file for `non_regression` returns zero hits before fix, one hit after fix.

### Phase 2 — Fix failing tests

4. **Fix any import or path errors in unit tests**
   - File: individual test files identified in task 2
   - Changes: Correct wrong module paths, add missing conftest fixtures, ensure `KEY_REGISTRY_ENCRYPTION_KEY` and `KEY_REGISTRY_DB_PATH` are set before any test that needs them (most are covered by `_clear_settings_cache` + fixture `monkeypatch.setenv`, but some direct imports may escape).
   - Acceptance: All tests in `uv run pytest -m "not slow and not non_regression"` pass with zero failures.

5. **Ensure test-fast runs without model download, network, or Docker**
   - File: `tests/conftest.py`, `tests/test_stt.py`, `tests/test_download.py`
   - Changes: Verify the `mock_engine` fixture fully replaces `faster_whisper.WhisperModel` (no real import reaches HuggingFace). Verify `test_download.py` mocks the network calls. **Fix the `app` fixture in `conftest.py`**: currently it calls `create_app()` before overriding `application.state.engine = mock_engine`, which means the lifespan startup phase triggers `ensure_whisper_model()` → `snapshot_download()` → hits HuggingFace Hub (~1.5 GB download on first run). Reorder so the mock engine is injected before lifespan runs, or mock `ensure_whisper_model` in the fixture.
   - Acceptance: Run `uv run pytest -m "not slow and not non_regression"` with network disabled (`unshare -n` or firewall rule) — all tests pass.

6. **Fix mypy strict-mode violations if any exist**
   - File: `src/compadre/**/*.py` (any file with type errors)
   - Changes: Run `uv run mypy src/` and fix every error. Pay special attention to `faster_whisper` stubs (already in `[[tool.mypy.overrides]]` with `ignore_missing_imports = true` — confirm no other untyped third-party packages slip through).
   - Acceptance: `uv run mypy src/` exits zero.

7. **Fix ruff lint AND format violations**
   - File: any Python file flagged by `uv run ruff check .` or `uv run ruff format --check .`
   - Changes: Fix all lint violations with `uv run ruff check --fix .`. **Also fix formatting violations** — currently `ruff format --check .` fails on 5 files (`scripts/capacity_test.py`, `scripts/loadtest.py`, `tests/test_formatters.py`, `tests/test_preprocessing_audio.py`, `tests/test_stt.py`). Run `uv run ruff format .` to auto-fix.
   - Acceptance: `uv run ruff check .` exits zero; `uv run ruff format --check .` exits zero.

### Phase 3 — Restructure CI into parallel jobs

8. **Add dependency groups for CI-specific tools to pyproject.toml**
   - File: `pyproject.toml`
   - Changes: Add a `[dependency-groups]` entry `ci` that includes `ansible-lint>=24`, `yamllint>=1.35`. (Other tools — hadolint, shellcheck, markdownlint — are run via pre-commit hooks that self-provision binaries, so they don't need to be pip-installed.) Optionally add `ansible` to the ci group if ansible-lint needs it.
   - Acceptance: `uv sync --group ci` installs ansible-lint and yamllint without errors.

9. **Rewrite `.github/workflows/ci.yml` with parallel jobs**
   - File: `.github/workflows/ci.yml`
   - Changes: Replace the single `ci` job with the following parallel jobs. Each job shares a common setup via a reusable composite action or an inline step block:

   | Job | Runs | Timeout | Needs Docker |
   |-----|------|---------|--------------|
   | `lint` | ruff check + ruff format --check + pre-commit (hadolint, shellcheck, yamllint, markdownlint) | 5 min | No |
   | `type-check` | mypy src/ | 5 min | No |
   | `test-fast` | pytest -m "not slow and not non_regression" | 10 min | No |
   | `test-non-regression` | pytest -m non_regression | 10 min | Yes |
   | `ansible-lint` | ansible-lint + yamllint on deploy/ansible/ | 5 min | No |
   | `docker-build` | docker build + hadolint (repeat for signal isolation) | 10 min | Yes |
   | `coverage` | pytest with coverage, upload artifact (depends on test-fast) | 10 min | No |

   Use `actions/checkout@v4`, `astral-sh/setup-uv@v5` with `enable-cache: true`, and `uv sync --group test --group ci` (or just `--group test` for jobs that don't need ansible-lint).

   For `docker-build`: use `docker/build-push-action@v6` with `push: false` to verify the image compiles. Run `hadolint` directly (install via `hadolint/hadolint-action@v3` or `brew install hadolint` on the runner).

   For `lint`: run `uv run pre-commit run --all-files` to pick up hadolint, yamllint, shellcheck, markdownlint from `.pre-commit-config.yaml`. Still run `ruff check` and `ruff format --check` directly for clearer error messages.

   For `test-non-regression`: this job needs Docker. Run `uv sync --group test` + `uv run pytest -m non_regression tests/test_non_regression/ -v`. The conftest's `compose_stack` fixture will spawn the Caddy+nginx stack. The job's `timeout-minutes` should include time for `docker pull`.

   - Acceptance: Each job runs independently. A lint failure does not block type-check results. A test-fast failure does not block non-regression results. All 7 jobs appear in the GitHub Actions UI as separate check runs.

10. **Add job-level caching for uv and Docker layers**
    - File: `.github/workflows/ci.yml`
    - Changes: `astral-sh/setup-uv@v5` with `enable-cache: true` already caches uv packages. For Docker builds, use `docker/build-push-action@v6` with GitHub Actions cache backend (`cache-from: type=gha`, `cache-to: type=gha,mode=max`). For the non-regression job, pre-pull `caddy:2-alpine` and `nginx:alpine` into the Docker cache to avoid DockerHub throttling.
    - Acceptance: Second CI run on the same commit is significantly faster (<50% of first-run wall time). Docker images are pulled from cache, not from DockerHub.

### Phase 3.5 — Codebase cleanup (stale TTS artifacts)

The following stale references from the prior Comadre TTS project remain in the codebase and should be cleaned up as part of the CI pass (they trigger false positives in static scans and confuse reviewers):

| File | Stale reference | Fix |
|------|----------------|-----|
| `src/compadre/exceptions.py` | `VoiceNotFoundError` (mentions `kokoro/<name>`, `qwen/<name>`) | Rename to `ModelNotFoundError` or remove if unused |
| `src/compadre/exceptions.py` | `GenerationError` (mentions "TTS generation fails") | Rename to `TranscriptionError` or remove if unused |
| `src/compadre/main.py` | Comment referencing Qwen3 char-limit | Update comment to STT context |
| `src/compadre/keys/cli/commands/keys.py` | Comment referencing "TTS-runtime fields (Kokoro/Qwen3 paths)" | Update comment |
| `tests/test_non_regression/conftest.py` | Docstring referencing "Kokoro/Qwen3 model-download cost" | Update docstring |
| `tests/test_non_regression/README.md` | Multiple `tts-service`, `Kokoro + Qwen3`, `/v1/audio/speech` refs | Update to `compadre-stt`, STT terminology, `/v1/audio/transcriptions` |
| `tests/test_non_regression/test_no_raw_httpexception.py` | Remediation hint suggesting `VoiceNotFoundError` / `GenerationError` for 404/500 | Update hint to use STT-appropriate error classes |
| `deploy/caddy/Caddyfile` | Comment "API paths → tts-service" | Update to "compadre-stt" |
| `src/compadre/download.py` | Raw `os.environ` usage (violates AGENTS.md "no raw `os.environ`" rule) | Route through `config.py` or accept as justified for `HF_HOME` default |
| `src/compadre/rate_limit.py` | Module docstring references `/v1/audio/speech` | Update to `/v1/audio/transcriptions` |

### Phase 4 — Non-regression gates

11. **Ensure the `non_regression` marker is the sole gate for the non-regression job**
    - File: `.github/workflows/ci.yml` (test-non-regression job), `pyproject.toml`
    - Changes: The `test-non-regression` job runs `uv run pytest -m non_regression -v`. The default `addopts` in `pyproject.toml` stays `-m 'not non_regression'` so developer `pytest` never triggers docker downloads or slow scans. The `grep` invariant from the non-regression README (`rg pytest.mark.non_regression tests/ | grep -v test_non_regression/` must return empty) is enforced — add a CI step that runs this grep and fails if any non-regression test lives outside `tests/test_non_regression/`.
    - Acceptance: The non-regression job runs exactly the 7 files in `tests/test_non_regression/`. No other test files carry the `non_regression` marker. CI step for the grep invariant passes.

12. **Add the three static-scan non-regression invariants as dedicated CI checks**
    - File: `.github/workflows/ci.yml` (could stay within `test-non-regression` or be promoted to lint)
    - Changes: The three static-scan tests (no-raw-HTTPException, timing-safe-compare, SQL-static-bandit) run as part of `test-non-regression`. They don't need Docker — they could be promoted to the `lint` job for faster feedback, but keeping them in `test-non-regression` respects the existing contract (one marker, one job). Document this design choice.
    - Acceptance: The three tests pass in the non-regression job. The remediation messages in assertion failures name the exact file, line, and fix.

13. **Add the Fernet decrypt invariant check (new non-regression test)**
    - File: new `tests/test_non_regression/test_fernet_decrypt_invariant.py`
    - Changes: Static AST scan that greps `src/compadre/` for `fernet\.decrypt` and asserts exactly one hit in `src/compadre/keys/crypto.py`. This enforces the single-Fernet-invariant from the architecture doc.
    - Acceptance: The test passes in CI. If someone adds a second `fernet.decrypt` outside `crypto.py`, CI fails with a clear message naming the violating file and line.

14. **Add the no-raw-os.environ invariant check (new non-regression test)**
    - File: new `tests/test_non_regression/test_no_raw_os_environ.py`
    - Changes: Static AST scan that walks `src/compadre/` and forbids `os.environ[...]`, `os.getenv(...)`, and `from os import environ` outside of `config.py`. Config is the sole pydantic-settings consumer — everything else must read from the `Settings` model.
    - Acceptance: The test passes in CI. Violations surface with file+line+remediation hint.

### Phase 5 — Coverage reporting

15. **Add coverage collection to the test-fast job**
    - File: `.github/workflows/ci.yml`
    - Changes: The `coverage` job (or a step within `test-fast`) runs `uv run coverage run -m pytest -m "not slow and not non_regression" --tb=short -q` followed by `uv run coverage report --fail-under=80`. Exclude test files, migration scripts, and `__init__.py` files via `.coveragerc` or `[tool.coverage]` in `pyproject.toml`. The fail-under threshold starts at 60% (current coverage unknown) and increments in follow-up stories toward 80%.
    - Acceptance: `coverage` job produces a terminal report and a `coverage.xml` artifact. The job fails if coverage drops below threshold.

16. **Create `.coveragerc` configuration**
    - File: new `.coveragerc` (or `[tool.coverage.run]` in `pyproject.toml`)
    - Changes: Set `source = src/compadre`, `omit = */tests/*, */migrations/*, */__init__.py`. Set `branch = true` for branch coverage.
    - Acceptance: `uv run coverage report` shows only application code. Test helpers, fixtures, and migrations are excluded.

### Phase 6 — Documentation

17. **Write CI runbook**
    - File: new `docs/ci-runbook.md`
    - Changes: Document each CI job, its purpose, its dependencies, the commands it runs, common failure modes, and how to reproduce locally. Include the `scripts/ci-local.sh` invocation. Document the GitHub Actions UI layout (7 parallel jobs). Include a troubleshooting section for DockerHub rate-limiting, uv cache poisoning, and slow test timeouts.
    - Acceptance: A new team member can read the runbook and understand what each check enforces and how to debug a red pipeline.

18. **Update AGENTS.md with CI conventions**
    - File: `/home/galvani/dev/compadre/AGENTS.md`
    - Changes: Add a CI section documenting the parallel job structure, the non-regression marker contract, the coverage threshold, and the invariant that all quality checks must pass before merge. Reference the runbook.
    - Acceptance: The CI section is clear enough that an LLM agent reading this project for the first time understands how to keep CI green.

---

## Files to Modify

- `.github/workflows/ci.yml` — complete rewrite from monolithic to 7 parallel jobs
- `pyproject.toml` — add `ci` dependency group (ansible-lint, yamllint), add `[tool.coverage.run]` or keep `.coveragerc` separate, update `addopts` if needed
- `AGENTS.md` — add CI section
- `tests/conftest.py` — ensure fixtures are robust for CI env (no assumption of writable `/tmp` beyond `tmp_path`)
- `tests/test_stt.py` — verify all imports are mocked, zero real model access
- `tests/test_download.py` — verify network calls are mocked
- Any test files with failures identified in Phase 1 diagnosis

## New Files

- `scripts/ci-local.sh` — shell script to reproduce CI locally
- `.coveragerc` — coverage configuration (or inline in `pyproject.toml`)
- `tests/test_non_regression/test_fernet_decrypt_invariant.py` — new non-regression test for single-Fernet invariant
- `tests/test_non_regression/test_no_raw_os_environ.py` — new non-regression test for no-raw-os.environ invariant
- `docs/ci-runbook.md` — CI runbook and troubleshooting guide
- `docs/plan/ci-diagnosis.md` — scratch diagnostic notes (transient, deleted after fixes)

## Dependencies

- **Task 1 → Task 2**: Must reproduce current failures before triaging
- **Task 2 → Tasks 4–7**: Must know what's broken before fixing
- **Tasks 4–7 → Task 9**: Fixes must land before the CI rewrite goes green
- **Task 8 → Task 9**: CI dependency groups must exist before the rewrite uses them
- **Task 9 → Tasks 11–14**: Non-regression job must exist before adding new invariants
- **Task 9 → Task 15**: Coverage job must exist before threshold enforcement
- **Task 9 → Task 17**: CI structure must be stable before documenting it
- **Tasks 1–17 are sequential within phases but phases 3–5 can partially overlap once fixes land**

## Risks

1. **DockerHub rate-limiting**: The non-regression job pulls `caddy:2-alpine` and `nginx:alpine`. GitHub Actions runners share IPs and can hit DockerHub pull limits. Mitigation: use `docker/build-push-action` cache, pin image digests in the conftest, or host images in GHCR. The `compose_stack` fixture already has `_prepull_images()` — ensure it retries on 429 and respects `DOCKER_HUB_TOKEN` if set.

2. **Docker-in-Docker on GitHub Actions**: GitHub-hosted runners support Docker natively (the daemon runs on the host). The `compose_stack` fixture binds ports 80/443 on `127.0.0.1` — this works on GitHub Actions but verify there's no port conflict with other runners or services. The `_tear_down_stack()` fixture is idempotent and runs in `finally:`, so leaked containers from prior runs are cleaned up.

3. **uv cache across jobs**: `astral-sh/setup-uv@v5` with `enable-cache: true` uses GitHub's `actions/cache`. The cache key includes the OS, Python version, and lockfile hash. If the cache grows stale (different lockfile), the first run will be slow. Acceptable.

4. **ansible-lint may need ansible-core**: ansible-lint depends on `ansible-core` for playbook parsing. The `community.docker` and `community.general` collections in `requirements.yml` are only needed for execution, not linting. Ensure ansible-lint runs without the collections installed by using `--skip-list syntax-check` if needed, or install the collections in CI.

5. **Coverage threshold unknown**: The current test coverage percentage is unknown. Setting `--fail-under=80` immediately may be unrealistic. Start with a diagnosis run (output-only, no fail-under), then set the threshold to `current - 2%` to prevent regression. Increment in follow-up stories.

6. **Shell script edge cases in `ci-local.sh`**: The script must handle: missing ffmpeg (install prompt), missing uv (install prompt), stale venv (auto-sync). Keep it simple — fail with clear messages, don't auto-install system packages.

7. **hadolint in CI via pre-commit**: The pre-commit hook for hadolint downloads the hadolint binary. GitHub Actions runners have internet access, so this works. But if the pre-commit cache is cold, it adds ~10s per run. Acceptable; pre-commit's own cache mitigates this.

8. **The `Caddyfile-no-landing` divergence**: The non-regression tests (`test_security_headers.py` and `test_request_body_limit.py`) assert parity between `Caddyfile` and `Caddyfile-no-landing`. The two files currently diverge on CSP, body-framing envelope format, and the `handle_errors` block. If the no-landing variant drifts further, these tests will fail. The current tests are parametrized with both names and will catch drift.

## Validation Checklist

After all tasks are complete, verify:

- [ ] `uv run pytest -m "not slow and not non_regression"` passes with zero failures locally
- [ ] `uv run pytest -m non_regression tests/test_non_regression/` passes with docker running
- [ ] `uv run ruff check . && uv run ruff format --check .` pass
- [ ] `uv run mypy src/` passes
- [ ] `uv run pre-commit run --all-files` passes (hadolint, yamllint, shellcheck, markdownlint)
- [ ] `ansible-lint deploy/ansible/playbooks/` passes
- [ ] `docker build .` succeeds
- [ ] `uv run coverage run -m pytest ... && uv run coverage report` shows ≥ threshold coverage
- [ ] All 7 CI jobs appear green on a push to a PR branch
- [ ] Each CI job completes in under 5 minutes (except test-non-regression and docker-build: ≤10 min)
- [ ] The `grep` invariant (`rg pytest.mark.non_regression tests/ | grep -v test_non_regression/`) returns empty
- [ ] The `grep` invariant (`rg "fernet\.decrypt" src/compadre/`) returns exactly one hit in `crypto.py`
- [ ] CI runbook is clear and actionable
