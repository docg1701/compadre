# Ruff Lint Fixes

## Summary

Fixed all 13 ruff lint errors across 10 files. No logic changed — only formatting, line breaks, and trivial fixes.

## Changes

| # | Error | File | Fix |
|---|-------|------|-----|
| 1 | B008 | `src/compadre/api.py:56` | Added `# noqa: B008` comment (FastAPI standard pattern for `File(...)`) |
| 2 | E501 (93→88) | `src/compadre/api.py:103` | Broke f-string `hint=` across 3 lines |
| 3 | E501 (95→88) | `src/compadre/api.py:143` | Broke `hint=` string across 2 lines |
| 4 | E501 (91→88) | `src/compadre/config.py:191` | Broke `description=` string across 3 lines |
| 5 | E501 (94→88) | `src/compadre/formatters.py:69` | Split f-string into two implicit-concatenated f-strings |
| 6 | E501 (89→88) | `src/compadre/keys/cli/commands/billing.py:99` | Wrapped single-line string in parentheses over 2 lines |
| 7 | E501 (89→88) | `src/compadre/keys/cli/commands/keys.py:415` | Split string across 2 implicit-concatenated lines inside parens |
| 8 | F841 | `src/compadre/preprocessing.py:51` | Renamed `result` → `_result` (unused variable) |
| 9 | E501 (89→88) | `src/compadre/stt.py:46` | Split log message string across 2 implicit-concatenated lines |
| 10 | E501 (90→88) | `tests/test_exceptions.py:27` | Broke dict literal across multiple lines |
| 11 | E501 (89→88) | `tests/test_non_regression/test_rate_limit_wired.py:1` | Split docstring first line across 2 lines |
| 12 | E501 (96→88) | `tests/test_non_regression/test_request_body_limit.py:105` | Broke `probe_client.post(...)` call across multiple lines |

## Verification

- `uv run ruff check .` — All checks passed
- `uv run ruff format --check .` — 112 files already formatted
- `uv run pytest -q` — 195 passed, 42 deselected
