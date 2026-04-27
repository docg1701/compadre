# Worker Tests — Compadre STT Unit Tests

## Summary

Three new test files created under `tests/` covering the STT engine, output formatters, and audio preprocessing. All 32 tests pass. No real external dependencies — everything mocked.

## Files Created

### 1. `tests/test_stt.py` — WhisperEngine (10 tests)

| Test class | Tests | What's covered |
|---|---|---|
| `TestLoadModel` | 2 | Mock `faster_whisper.WhisperModel` constructor; success stores instance + flips `is_loaded`; construction failure propagates |
| `TestWarmUp` | 3 | Delegates to `_model.transcribe` with a WAV path; raises `CompadreError` if model unloaded; survives internal transcription failure (logs warning, doesn't crash) |
| `TestTranscribe` | 5 | Delegates to `_model.transcribe` + returns segments/info tuple; raises `CompadreError` before load; raises `InvalidRequestError` for missing/empty file; wraps upstream failure in `CompadreError` |

**Mock strategy:** `patch("faster_whisper.WhisperModel")` for constructor (imported inside `load_model` at call time). `_model` is a `MagicMock` whose `transcribe.return_value` is `(iter([...]), info_mock)`.

### 2. `tests/test_formatters.py` — Output Formatters (12 tests)

| Test class | Tests | What's covered |
|---|---|---|
| `TestFormatText` | 2 | Joins stripped text; empty list → `""` |
| `TestFormatJson` | 2 | Returns `{"text": ...}`; empty string → `{"text": ""}` |
| `TestFormatVerboseJson` | 3 | Full structure: text, language, duration, segments with word-level timestamps; `words=None` → `[]`; empty segments |
| `TestFormatSrt` | 3 | Correct SRT numbering, timestamps (HH:MM:SS,mmm), blank-line separators; strips whitespace; empty → `""` |
| `TestFormatVtt` | 2 | WEBVTT header + period-separated timestamps; empty → `"WEBVTT\n"` |

**Helper:** `_make_segment(text, start, end, words=...)` builds a `MagicMock` with `.text`, `.start`, `.end`, `.words` (list of `_make_word()` mocks or `None`).

### 3. `tests/test_preprocessing_audio.py` — Audio Prep (10 tests)

| Test class | Tests | What's covered |
|---|---|---|
| `TestDecodeAudioToWav` | 5 | Success path returns output path; `CalledProcessError` → `InvalidRequestError`; missing/empty output file → `InvalidRequestError`; `TimeoutExpired` → `InvalidRequestError` |
| `TestGetAudioDuration` | 5 | Parses float from stdout; non-zero returncode → `0.0`; empty stdout → `0.0`; non-numeric stdout (`ValueError`) → `0.0`; `TimeoutExpired` → `0.0` |

**Mock strategy:** `patch("subprocess.run")` with `MagicMock` return values. Files created via `tmp_path` fixture (pytest). Note: `get_audio_duration` returns `0.0` on failure, not `None` — tests match actual behavior.

## Verification

```bash
uv run pytest tests/test_stt.py tests/test_formatters.py tests/test_preprocessing_audio.py -v
# 32 passed in 0.50s
```

Full suite still green:

```bash
uv run pytest tests/test_api.py -v
# 9 passed
```
