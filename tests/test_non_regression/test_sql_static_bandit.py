"""SQL-injection non-regression — bandit B608 static check (Story 4.5g).

Subprocess-invokes ``bandit -r src/compadre/ -t B608 -ll -c <isolation> -f
json -q`` and asserts no findings. Severity gate: MEDIUM-or-higher (B608's
max severity is MEDIUM; HIGH-only would catch zero SQL-injection bugs).

Config isolation via ``bandit_isolation.yaml`` short-circuits any future
``[tool.bandit]`` ``skips = ["B608"]`` line in ``pyproject.toml``.

The B608 sites under ``src/compadre/keys/db.py`` (column-allowlist f-string
splicing in UPDATE/SELECT/IN-clause) carry ``# nosec B608  # <reason>``
two-comment annotations — bandit reads only the test-id after ``# nosec``,
so the reason text never reaches its tolerant lexer.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


def _find_project_root() -> Path:
    """Walk parents until a directory with ``pyproject.toml`` is found.
    Pinning by sentinel-file prevents silent scope drift on file relocation."""
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "pyproject.toml").is_file():
            return candidate
    raise RuntimeError(f"could not locate project root from {here}")


_PROJECT_ROOT: Path = _find_project_root()
_SRC_KEYS_ROOT: Path = _PROJECT_ROOT / "src" / "compadre"
_FIXTURES_ROOT: Path = (
    Path(__file__).resolve().parent / "fixtures" / "sql_static_bandit"
)
_BANDIT_CONFIG: Path = _FIXTURES_ROOT / "bandit_isolation.yaml"
_BANDIT_TEST_ID: str = "B608"


@dataclass(frozen=True)
class _Finding:
    test_id: str
    filename: str
    line_number: int
    severity: str
    confidence: str
    code: str

    def render(self) -> str:
        path = Path(self.filename)
        try:
            rel = path.relative_to(_PROJECT_ROOT)
        except ValueError:
            rel = path
        excerpt = self.code.strip().splitlines()
        snippet = excerpt[len(excerpt) // 2] if excerpt else ""
        return (
            f"{rel}:{self.line_number}: [{self.test_id}] "
            f"severity={self.severity} confidence={self.confidence} — "
            f"{snippet.strip()}"
        )


def _bandit_importable() -> bool:
    """``python -m bandit --version`` — robust against ``$PATH`` shims."""
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "bandit", "--version"],
            capture_output=True,
            check=False,
            timeout=10,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def _run_bandit(paths: list[Path]) -> list[_Finding]:
    """Invoke bandit on ``paths``; skip on missing bandit unless under CI
    (CI fails loudly so a dropped ``security`` dep cannot fail-open)."""
    if not _bandit_importable():
        msg = "bandit not importable; run `uv sync` to install the security group."
        if os.getenv("CI"):
            pytest.fail(msg)
        pytest.skip(msg)

    missing = [p for p in paths if not p.exists()]
    if missing:
        pytest.fail(f"bandit input path(s) missing: {[str(p) for p in missing]}")

    cmd = [
        sys.executable,
        "-m",
        "bandit",
        "-c",
        str(_BANDIT_CONFIG),
        "-r",
        *[str(p) for p in paths],
        "-t",
        _BANDIT_TEST_ID,
        "-ll",
        "-f",
        "json",
        "-q",
    ]
    try:
        completed = subprocess.run(
            cmd,
            cwd=_PROJECT_ROOT,
            capture_output=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        pytest.fail(f"bandit exceeded 60s timeout: {exc}")

    # bandit: 0=clean, 1=findings, 2=usage error.
    if completed.returncode not in (0, 1):
        pytest.fail(
            f"bandit unexpected exit={completed.returncode}\n"
            f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
        )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        pytest.fail(f"bandit stdout not JSON: {exc}\nstdout:\n{completed.stdout}")

    if "results" not in payload:
        pytest.fail(f"bandit JSON missing 'results' key: {sorted(payload)}")

    findings: list[_Finding] = []
    for result in payload["results"]:
        if result.get("test_id") != _BANDIT_TEST_ID:
            continue
        try:
            findings.append(
                _Finding(
                    test_id=result["test_id"],
                    filename=result["filename"],
                    line_number=int(result["line_number"]),
                    severity=result["issue_severity"],
                    confidence=result["issue_confidence"],
                    code=result.get("code", ""),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            pytest.fail(f"unexpected bandit JSON shape: {exc}; result={result!r}")
    return findings


@pytest.mark.non_regression
def test_src_compadre_keys_has_no_b608_findings() -> None:
    """Non-regression: bandit B608 reports zero findings across all of
    ``src/compadre/``. New raw f-string / %% / + SQL → red; controlled
    column splicing → annotate ``# nosec B608  # <reason>``."""
    findings = _run_bandit([_SRC_KEYS_ROOT])
    assert not findings, (
        "bandit B608 (hardcoded_sql_expressions) reported findings.\n"
        "Use parameterised queries: conn.execute('… WHERE x = ?', (value,))\n"
        "Or annotate controlled splicing:\n"
        "  # nosec B608  # <which collection, why it's safe>\n\n"
        "Findings:\n  " + "\n  ".join(f.render() for f in findings)
    )


@pytest.mark.non_regression
def test_scan_self_tests_flag_violations_and_respect_allowlists() -> None:
    """Self-test: bandit catches f-string + %-format SQL; ``# nosec B608``
    suppresses; positional ``?`` produces zero findings."""
    f = _run_bandit([_FIXTURES_ROOT / "bad_fstring_sql.py"])
    assert len(f) == 1 and f[0].test_id == "B608"

    pct = _run_bandit([_FIXTURES_ROOT / "bad_concat_sql.py"])
    assert len(pct) == 1 and pct[0].test_id == "B608"

    assert _run_bandit([_FIXTURES_ROOT / "nosec_allowed_sql.py"]) == []
    assert _run_bandit([_FIXTURES_ROOT / "parameterised_sql.py"]) == []
