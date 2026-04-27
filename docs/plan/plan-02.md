# Implementation Plan 02 — API Functional Validation

## Goal

Validate every API endpoint and feature against a living Compadre instance: run the full mocked unit suite green, then add real-model integration tests (`@pytest.mark.slow`) covering every format × audio type × auth scenario × limit scenario cell. Fix any broken behavior surfaced by the validation.

## Current State (2026-04-26 baseline)

The existing mocked test suite is already deep:

| Area | Coverage in existing tests | Gaps |
|------|----------------------------|------|
| Response formats (unit) | `test_formatters.py` — all 5 formats with mock segments | None — thorough |
| Response formats (HTTP) | `test_api.py::test_transcription_happy_path_all_formats` — all 5 via ASGI with mocked engine | None |
| Audio preprocessing (mocked) | `test_preprocessing_audio.py` — ffmpeg decode + ffprobe duration, all edge cases | None |
| STT engine (mocked) | `test_stt.py` — load/warm-up/transcribe, all error paths | None |
| Auth (basic shape) | `test_api.py` — unknown, malformed, suspended, revoked, expired, valid active, valid complimentary | Missing: per-IP rate limit on `/health` auth probe (enumeration-resistance envelope shape assertions already exist in `test_registry_auth_unknown_or_malformed_401_envelope`) |
| Rate limiting | `test_rate_limit.py` — per-key RPM, independent buckets, error on missing state | Missing: `X-RateLimit-*` header assertions on allow, `Retry-After` on 429, per-IP `/health` limit, per-IP transcription limit |
| Upload limits | `test_api.py::test_transcription_file_too_large_400` | Missing: duration > 10 min, boundary tests at exactly 25 MB / exactly 10 min |
| Health endpoint | `test_health.py` — full vs minimal shape, 503 warming | None — thorough |
| Error envelope | `test_api.py::test_transcription_405_envelope_on_get_and_post_health` — 405 covered | Missing: 404 envelope, unhandled 500 via `Exception` handler |
| Queue behavior | `test_queue.py` — sequential processing, depth increment/decrement | None |
| Schemas | `test_schemas.py` — ResponseFormat enum | None |
| Non-regression | `tests/test_non_regression/` (7 files) | Failing in CI/local due to missing Docker/Caddy |
| Real-model integration | **None** — zero `@pytest.mark.slow` tests | Everything needed |

## Success Criteria

1. `uv run pytest -m "not slow" --tb=short -q` exits 0 with zero failures (skips for non-regression OK)
2. `uv run pytest -m slow --tb=short -q` exits 0 at least once with real model loaded, covering the full test matrix
3. Every response format (`text`, `json`, `verbose_json`, `srt`, `vtt`) returns correct output for a real audio file
4. Every supported audio input format (ogg/opus, mp3, m4a, wav, webm) is accepted and transcribed
5. Every auth failure mode returns 401 with correct envelope, enumeration-resistant
6. Rate limiting returns 429 at `rate_limit_rpm + 1` with correct `X-RateLimit-*` and `Retry-After` headers
7. Per-IP rate limits on `/health` (60/min) and `/v1/audio/transcriptions` (600/min) trigger correctly
8. Size > 25 MB and duration > 10 min both return proper error envelope
9. All error envelopes contain exactly `{"error": {"message", "type", "hint"}}`; no raw tracebacks
10. Temp files are cleaned up after both successful and failed transcriptions

## Prerequisites

- Plan 1 completed (docs define correct expected behavior)
- `faster-whisper large-v3-turbo` model downloaded (at least once) to `HF_HOME`
- `ffmpeg` and `ffprobe` on `$PATH` (already in dev container; CI validates)
- Docker daemon available (for non-regression tests; skip gracefully if absent)

## Tasks

### 1. Fix any failing existing mocked tests

- **Files**: All files with failures from `uv run pytest -m "not slow"`
- **Changes**:
  - Run `uv run pytest -m "not slow" --tb=long -q` and triage every failure
  - Per checkpoint 2026-04-26, the non-regression tests (`test_security_headers.py`, `test_request_body_limit.py`, etc.) fail with `ConnectionRefusedError` because they need Caddy running
  - For Docker-dependent non-regression tests: add a `pytest.skipif` guard in `tests/test_non_regression/conftest.py` that checks for Docker and Caddy availability; skip with a clear message when unavailable
  - Document in the non-regression `README.md` how to run those tests: `docker compose up -d && uv run pytest tests/test_non_regression/`
- **Acceptance**: `uv run pytest -m "not slow" --tb=short -q` exits 0 (skips allowed)

### 2. Add `pytest.mark.slow` marker infrastructure

- **Files**: `tests/conftest.py`, `pyproject.toml`
- **Changes**:
  - Verify `slow` marker is registered in `pyproject.toml` `[tool.pytest.ini_options].markers` — it is (line: `"slow: integration tests requiring model files"`)
  - Verify `pytest_configure` in `conftest.py` registers the marker — it does
  - Add a `pytest.fixture(scope="session")` in `conftest.py` that loads the real `WhisperEngine` once per test session:
    ```python
    @pytest.fixture(scope="session")
    def real_engine() -> WhisperEngine:
        """Load the real Whisper model once per test session (shared across slow tests)."""
        engine = WhisperEngine()
        engine.load_model()
        return engine
    ```
  - Add a `pytest.fixture` for a reference audio file path (the existing `audio-de-teste-01.ogg` at the repo root)
  - Add a `pytest.fixture(scope="session")` that generates test audio files in all 5 formats (ogg/opus, mp3, m4a, wav, webm) from `audio-de-teste-01.ogg` using `ffmpeg` subprocess, stored in a temp directory
- **Acceptance**: `uv run pytest -m slow --co` lists new slow tests when added; session fixtures produce real model and audio files

### 3. Write real-model response format integration tests

- **File**: `tests/test_integration_response_formats.py` (new)
- **Changes**:
  - Create `TestRealModelResponseFormats` class
  - For each `response_format` in `text`, `json`, `verbose_json`, `srt`, `vtt`:
    1. Decode `audio-de-teste-01.ogg` to WAV via `preprocessing.decode_audio_to_wav()`
    2. Call `real_engine.transcribe(wav_path, language="pt")`
    3. Assert segments returned is a list, info has `language` attribute
    4. Format via the appropriate formatter (`format_text`, `format_json`, `format_verbose_json`, `format_srt`, `format_vtt`)
    5. Assert output structure:
       - `text`: non-empty string, no JSON wrapper
       - `json`: `{"text": "..."}` with string value
       - `verbose_json`: `{"text", "language", "duration", "segments"}` where each segment has `{"start", "end", "text", "words"}`
       - `srt`: lines match `^\\d+$`, `^\\d{2}:\\d{2}:\\d{2},\\d{3} --> \\d{2}:\\d{2}:\\d{2},\\d{3}$`, non-empty text, blank line separators
       - `vtt`: starts with `"WEBVTT\n"`, timestamps use `.` not `,`
  - All tests decorated with `@pytest.mark.slow`
- **Acceptance**: Each format produces structurally valid output from real model inference

### 4. Write real-model audio format acceptance tests

- **File**: `tests/test_integration_audio_formats.py` (new)
- **Changes**:
  - Create `TestRealModelAudioFormats` class
  - Generate test audio files (or use session fixture from Task 2):
    1. `test.ogg` (OPUS in OGG container)
    2. `test.mp3`
    3. `test.m4a`
    4. `test.wav` (16kHz mono PCM)
    5. `test.webm` (OPUS in WebM container)
  - For each format:
    1. Run `decode_audio_to_wav(input_path, output_path)` — assert no exception
    2. Verify output WAV exists, is non-empty
    3. Call `real_engine.transcribe(wav_path, language="pt")`
    4. Assert transcription returns segments, non-empty text
    5. Log the transcription text for manual spot-check
  - Edge case: corrupted file (random bytes renamed `.ogg`) → `InvalidRequestError` from decode
  - Edge case: empty file → `InvalidRequestError` from engine
  - All tests decorated with `@pytest.mark.slow`
- **Acceptance**: All 5 supported audio formats decode and transcribe without error; corrupted/empty files rejected

### 5. Write real-model language detection test

- **File**: `tests/test_integration_language.py` (new)
- **Changes**:
  - Test explicit `language="pt"` → info reports `"pt"`, transcription is Portuguese
  - Test `language=None` (auto-detect) → info reports detected language, transcription matches
  - Test `language="en"` with Portuguese audio → verify engine receives `language="en"` (param passed through, even if mismatch)
  - Test with 1-second silence WAV (warm-up style) → returns empty segments, info still populated
  - All `@pytest.mark.slow`
- **Acceptance**: Language parameter passed through correctly, auto-detect works

### 6. Write end-to-end HTTP integration tests (via ASGI + real engine)

- **File**: `tests/test_integration_e2e.py` (new)
- **Changes**:
  - Build an `app` fixture variant that uses the real `WhisperEngine` instead of `mock_engine`
  - Create `TestE2ETranscription` class:
    1. Upload `audio-de-teste-01.ogg` via multipart → assert 200
    2. Test each `response_format` via HTTP, assert `Content-Type` + body structure
    3. Test `language=pt` form field is respected
    4. Test `model` form field is accepted (ignored)
    5. Test `temperature` form field is accepted (ignored)
  - Create `TestE2EAuth` class:
    1. Valid key → 200
    2. Missing `Authorization` → 401 with `authentication_error`
    3. `Bearer` with empty token → 401
    4. `Bearer wrong-shape` (not `cmpd_live_` prefix) → 401 enum-resistant
    5. `Basic` scheme → 401
    6. Suspended/revoked/expired key → 401 with correct hint
  - Create `TestE2ELimits` class:
    1. Audio > 25 MB → 400 with `invalid_request_error`
    2. Audio > 10 min → 400 with duration error
    3. Audio exactly 10 min (or 9:59) → 200 (boundary)
    4. Audio exactly 25 MB → 200 (boundary)
  - All `@pytest.mark.slow`
- **Acceptance**: End-to-end HTTP path works with real engine for all formats and auth scenarios

### 7. Fill rate-limiting header assertion gaps

- **File**: `tests/test_rate_limit.py` (extend existing)
- **Changes**:
  - Add `test_x_rate_limit_headers_on_allow()` — after a successful `check_rate_limit()`, verify `request.state.rate_limit_headers` contains `X-RateLimit-Limit`, `X-RateLimit-Remaining`, `X-RateLimit-Reset`
  - Add `test_retry_after_header_on_429()` — verify `RateLimitError` carries `retry_after`, and the error handler emits `Retry-After` header
  - Add `test_per_ip_health_rate_limit()` — call `check_health_per_ip_rate_limit` 61 times, assert 61st raises `RateLimitError`
  - Add `test_per_ip_transcription_rate_limit()` — call `check_transcription_per_ip_rate_limit` 601 times, assert 601st raises `RateLimitError`
  - Add `test_per_ip_health_limit_independent_from_transcription_limit()` — exhaust health IP bucket, verify transcription IP bucket still open
  - All using `RateLimiter.build()` + `await limiter.reset()` between cases
  - These are **mocked** tests (not slow) — no real model needed
- **Acceptance**: Rate-limiting tests cover per-key and per-IP buckets with header assertions

### 8. Fill auth edge-case gaps (mocked)

- **File**: `tests/test_api.py` (extend existing)
- **Changes**:
  - Add `test_auth_enumeration_resistance_all_401_identical_envelope()` — assert unknown key, malformed key, revoked key, suspended key all return `{"error": {"type": "authentication_error"}}` with identical `message` for unknown/malformed (per `ApiKeyNotFoundError` contract)
  - ~~Add `test_auth_expired_manual_key_401()`~~ ✅ Already exists as `test_registry_auth_suspended_revoked_expired_all_401_with_hint` in `tests/test_api.py`
  - ~~Add `test_auth_complimentary_key_no_paid_until_200()`~~ ✅ Already exists as `test_registry_auth_active_and_complimentary_keys_authenticate` in `tests/test_api.py`
  - Add `test_auth_complimentary_key_ignores_paid_until()` — even with expired `paid_until`, complimentary key still passes 200 (the existing complimentary test only uses `paid_until=None`)
  - ~~Add `test_operator_contact_hint_in_401()`~~ ✅ Already covered by `test_registry_auth_suspended_revoked_expired_all_401_with_hint` in `tests/test_api.py`
- **Acceptance**: Auth coverage matches all 6 states in spec

### 9. Add error envelope assertion helper and extend coverage

- **File**: `tests/test_error_envelope.py` (new)
- **Changes**:
  - Create `assert_error_envelope(response, expected_type, expected_status)` helper
  - Test 400 (`InvalidRequestError`) → envelope with `invalid_request_error`
  - Test 401 (all auth failures) → envelope with `authentication_error`
  - Test 404 (unknown path) → envelope via `GET /nonexistent` → `not_found_error`
  - Test 405 (wrong method) → `POST /health` → `method_not_allowed_error`
  - Test 429 (`RateLimitError`) → envelope with `rate_limit_error` + `Retry-After` header
  - Test 503 (`ServiceUnavailableError`) → envelope with `service_unavailable` + `Retry-After`
  - Test unhandled 500 → `Exception` handler → envelope with `server_error`, no raw traceback
  - Assert every error response body has exactly the shape `{"error": {"message": str, "type": str, "hint": str}}`
- **Acceptance**: All error paths produce the canonical envelope

### 10. Add upload limit boundary tests

- **File**: `tests/test_api.py` (extend existing)
- **Changes**:
  - Add `test_transcription_duration_too_long_400()` — create a WAV with > 10 min of silence, assert 400 with duration error message
  - Add `test_transcription_duration_exactly_10_min_200()` — 600.0-second WAV passes (if realistic with mocked engine)
  - Add `test_transcription_duration_at_boundary()` — 599s passes, 601s fails
  - Add `test_transcription_size_exactly_25_mb_200()` — exactly 25 MB file passes
  - Add `test_transcription_size_25mb_plus_1_byte_400()` — 25 MB + 1 byte fails
  - For duration tests, mock `get_audio_duration` to return controlled values; for size tests, generate byte arrays of exact sizes
- **Acceptance**: Boundary behavior is exact — off-by-one errors caught

### 11. Add temp file cleanup verification

- **File**: `tests/test_api.py` (extend existing)
- **Changes**:
  - Add `test_temp_files_cleaned_after_success()` — mock `NamedTemporaryFile` to track created paths; after 200 response, verify all temp paths are unlinked
  - Add `test_temp_files_cleaned_after_decode_failure()` — make `decode_audio_to_wav` raise; after 400, verify input temp file is cleaned up
  - Add `test_temp_files_cleaned_after_transcription_timeout()` — make engine raise `TimeoutError`; after 500, verify WAV temp file is cleaned up
  - Use `monkeypatch` on `Path.unlink` or a custom `TemporaryDirectory` tracker
- **Acceptance**: No temp file leaks on any code path

### 12. Verify and fix non-regression tests

- **File**: `tests/test_non_regression/conftest.py`, `tests/test_non_regression/README.md`
- **Changes**:
  - ✅ The `compose_stack` session fixture in `tests/test_non_regression/conftest.py` already checks for Docker CLI (`shutil.which("docker")`) and Docker daemon (`docker version`) and calls `pytest.skip()` when either is unavailable. Tests already skip gracefully — no new fixture needed.
  - Verify the `README.md` already documents the run command; update if necessary to reference the exact pytest invocation:
    ```bash
    docker compose up -d && uv run pytest -m non_regression tests/test_non_regression/ -v
    ```
- **Acceptance**: `uv run pytest tests/test_non_regression/` skips cleanly without Docker; runs all 7 tests with Docker up

### 13. Run full suite and validate

- **Changes**: Execute the full validation:
  1. `uv run ruff check .` — zero errors
  2. `uv run mypy src/` — zero errors
  3. `uv run pytest -m "not slow" --tb=short -q` — zero failures
  4. `uv run pytest -m slow --tb=short -q` — zero failures (run at least once with real model)
  5. Verify each cell in the test matrix (Section "Test Matrix" below) passes
- **Acceptance**: All checks green, every matrix cell covered

## Test Matrix

### Format × Audio Type (real model, `@slow`)

| Format ↓ / Audio → | ogg/opus | mp3 | m4a | wav | webm |
|---------------------|----------|-----|-----|-----|------|
| `text`              | ✅       | ✅  | ✅  | ✅  | ✅   |
| `json`              | ✅       | ✅  | ✅  | ✅  | ✅   |
| `verbose_json`      | ✅       | ✅  | ✅  | ✅  | ✅   |
| `srt`               | ✅       | ✅  | ✅  | ✅  | ✅   |
| `vtt`               | ✅       | ✅  | ✅  | ✅  | ✅   |

25 cells. Strategy: Test all formats against one audio type (ogg) at HTTP level; test all audio types against one format (`verbose_json`) at engine level. Cross-validate boundary: pick one format × one audio type for the full HTTP round-trip.

### Auth Scenario × Error Code (mocked)

| Scenario | Expected | Tested in |
|----------|----------|-----------|
| No Authorization header | 401 | `test_api.py` (existing) |
| `Bearer ` (empty) | 401 | `test_api.py::test_registry_auth_unknown_or_malformed_401_envelope` |
| `Bearer invalid-shape` (not `cmpd_live_`) | 401 | `test_api.py` (existing) |
| `Bearer cmpd_live_<43>` unknown hash | 401 | `test_api.py` (existing) |
| `Basic` scheme | 401 | `test_api.py` (existing) |
| Key status=suspended | 401 | `test_api.py` (existing) |
| Key status=revoked | 401 | `test_api.py` (existing) |
| Key paid_until past, manual | 401 | `test_api.py` (existing) |
| Key paid_until future, manual | 200 | `test_api.py` (existing) |
| Key complimentary, paid_until=NULL | 200 | `test_api.py` (existing) |
| Key complimentary, paid_until past | 200 | Task 8 (new) |

### Limit Scenario (mocked or generated)

| Limit | Expected | Tested in |
|-------|----------|-----------|
| RPM exceeded (per-key) | 429 + headers | `test_rate_limit.py` (existing + Task 7) |
| /health IP limit (60/min) | 429 | Task 7 (new) |
| /transcriptions IP limit (600/min) | 429 | Task 7 (new) |
| File > 25 MB | 400 | `test_api.py` (existing) + Task 10 boundary |
| File exactly 25 MB | 200 | Task 10 (new) |
| Duration > 10 min | 400 | Task 10 (new) |
| Duration exactly 10 min | 200 or 400 | Task 10 (new, verify spec) |
| Duration < 10 min | 200 | Task 10 (new) |
| Caddy Content-Length > 1 MiB | 413 (Caddy) | `test_request_body_limit.py` (existing non-regression) |

### Error Envelope (mocked)

| Status | error_type | Tested in |
|--------|-----------|-----------|
| 400 | `invalid_request_error` | Task 9 (new) |
| 401 | `authentication_error` | `test_api.py` + Task 9 |
| 404 | `not_found_error` | Task 9 (new) |
| 405 | `method_not_allowed_error` | `test_api.py` (existing) |
| 429 | `rate_limit_error` | Task 9 (new) |
| 500 | `server_error` | Task 9 (new) |
| 503 | `service_unavailable` | `test_api.py` (existing) + Task 9 |

## Files to Modify

| File | Changes |
|------|---------|
| `tests/conftest.py` | Add `real_engine` session fixture, reference audio fixture, multi-format audio generation fixture |
| `tests/test_api.py` | Extend: auth enumeration resistance, duration boundary tests, size boundary tests, temp file cleanup verification, complimentary-key-expired test |
| `tests/test_rate_limit.py` | Extend: X-RateLimit-* header assertions, Retry-After on 429, per-IP /health and /transcription limits |
| `tests/test_non_regression/conftest.py` | Add Docker/Caddy availability check + `pytest.skipif` hook |
| `tests/test_non_regression/README.md` | Document how to run non-regression tests |

## New Files

| File | Purpose |
|------|---------|
| `tests/test_integration_response_formats.py` | Real-model tests for all 5 `response_format` values (`@slow`) |
| `tests/test_integration_audio_formats.py` | Real-model tests for all 5 audio input formats (`@slow`) |
| `tests/test_integration_language.py` | Real-model language detection/override tests (`@slow`) |
| `tests/test_integration_e2e.py` | End-to-end HTTP + real engine tests: all formats, auth, limits (`@slow`) |
| `tests/test_error_envelope.py` | Canonical error shape assertions across all status codes (mocked, fast) |
| `tests/fixtures/README.md` | Documents test fixture audio files and how to regenerate them |

## Dependencies

- Plan 1 completed (docs define correct expected behavior)
- `faster-whisper` model downloaded (first `@slow` run downloads ~1.5 GB to `HF_HOME`)
- `ffmpeg` and `ffprobe` on `$PATH` (for generating test audio files in multiple formats)
- Docker daemon for non-regression tests (optional — skip gracefully if absent)
- Existing test infrastructure: `conftest.py` fixtures (`app`, `client`, `auth_headers`, `registry_conn`, `mock_engine`, `_keys_fixtures`), `seed_customer_and_key` helper

## Risks

| Risk | Mitigation |
|------|-----------|
| Real model download on first run takes 5-15 minutes (1.5 GB) | Document requirement; session-scoped fixture loads model once; pre-cache in CI via GitHub Actions cache step |
| `@slow` tests are too slow for PR CI (30-90s per test × 25+ tests) | Keep all real-model tests behind `@pytest.mark.slow`; only run in a separate CI job or nightly; fast suite stays fast |
| `ffmpeg` for generating test audio files may have codec-specific failures on some platforms | Use the simplest possible `ffmpeg` invocations; fall back to pre-generated committed test files if `ffmpeg` unavailable |
| Non-regression tests require Docker Compose (Caddy + compadre-stt containers) | Add `pytest.skipif` with clear message; document the docker-compose up command |
| Per-IP rate limit tests need IP variation to test independently | Use `X-Forwarded-For` header injection + unique IP per test case; reset `RateLimiter` storage between cases |
| Temp file cleanup tests are fragile (depend on internal implementation of `NamedTemporaryFile`) | Use `monkeypatch` on `tempfile.NamedTemporaryFile` to track created paths; verify deletion via `os.path.exists` |
| `verbose_json` format test could be flaky if real model produces zero segments for short audio | Use a known-good reference audio file (`audio-de-teste-01.ogg`) with non-trivial speech content |

## Validation Checklist

- [ ] `uv run ruff check .` clean
- [ ] `uv run mypy src/` clean
- [ ] `uv run pytest -m "not slow" --tb=short -q` exits 0, zero failures
- [ ] `uv run pytest -m slow --tb=short -q` exits 0, zero failures (at least one run with model)
- [ ] Every `response_format` value produces structurally valid output (auto test)
- [ ] Every supported audio input format decodes and transcribes (auto test)
- [ ] Language parameter: explicit `pt` works, `None` auto-detects (auto test)
- [ ] All auth failure modes return 401 with correct error envelope (auto test)
- [ ] Enumeration resistance: unknown key and malformed key produce identical 401 envelope (auto test)
- [ ] Operator contact hint appears in suspended/revoked 401 responses (auto test)
- [ ] Per-key RPM rate limit triggers at correct threshold (auto test)
- [ ] `X-RateLimit-*` headers present on allow; `Retry-After` on deny (auto test)
- [ ] Per-IP /health limit (60/min) triggers (auto test)
- [ ] Per-IP /transcriptions limit (600/min) triggers (auto test)
- [ ] File size > 25 MB → 400, exactly 25 MB → 200 (auto test)
- [ ] Duration > 10 min → 400, < 10 min → 200 (auto test)
- [ ] All error responses have canonical `{"error": {"message", "type", "hint"}}` (auto test)
- [ ] No raw tracebacks in 500 responses (auto test)
- [ ] 404 and 405 responses carry standard error envelope (auto test)
- [ ] Temp files cleaned up on success, decode failure, and timeout paths (auto test)
- [ ] Non-regression tests pass with Docker Compose running (manual or CI)
- [ ] Non-regression tests skip gracefully without Docker (auto test)
