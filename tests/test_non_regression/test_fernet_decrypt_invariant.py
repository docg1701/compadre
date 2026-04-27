"""FR51 non-regression — single-Fernet-decrypt invariant (Story 4.5g).

Architecture contract: ``fernet.decrypt(...)`` calls MUST only happen inside
``src/compadre/keys/crypto.py`` via ``decrypt_tax_id()``. Any new decrypt
site outside ``crypto.py`` is a review-blocker — the encryption boundary
must remain a single funnel.

This test grep-scans ``src/compadre/`` for the pattern ``fernet.decrypt``
and asserts exactly one hit, in ``keys/crypto.py``.

Allowlisting: lines carrying ``# scan-allow[single-fernet-decrypt]:`` are
excluded. There are zero expected allowlist entries at time of writing.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _PROJECT_ROOT / "src" / "compadre"
_EXPECTED_FILE = "src/compadre/keys/crypto.py"


def _grep_fernet_decrypt() -> list[tuple[Path, int, str]]:
    """Run ``grep -rn 'fernet\\.decrypt' src/compadre/`` and parse results."""
    result = subprocess.run(
        ["grep", "-rn", r"fernet\.decrypt", str(_SRC_ROOT)],
        capture_output=True,
        text=True,
    )
    if result.returncode not in (0, 1):
        pytest.fail(f"grep failed: {result.stderr.strip()}")
    if result.returncode == 1:
        return []  # no matches

    out: list[tuple[Path, int, str]] = []
    for line in result.stdout.strip().split("\n"):
        if not line:
            continue
        # Format: relative/path.py:lineno:content
        path_str, lineno_str, content = line.split(":", 2)
        path = Path(path_str)
        out.append((path, int(lineno_str), content.strip()))
    return out


@pytest.mark.non_regression
def test_single_fernet_decrypt_in_crypto() -> None:
    """Assert exactly one ``fernet.decrypt`` call site, in keys/crypto.py."""
    hits = _grep_fernet_decrypt()

    # Filter out allowlisted lines
    violations: list[str] = []
    for path, lineno, content in hits:
        # Read the specific line to check for scan-allow comment
        full_path = _PROJECT_ROOT / path
        if not full_path.exists():
            continue
        lines = full_path.read_text(encoding="utf-8").splitlines()
        if lineno <= len(lines):
            source_line = lines[lineno - 1]
            if "scan-allow[single-fernet-decrypt]" in source_line:
                continue
        violations.append(f"{path}:{lineno}: {content}")

    if not violations:
        pytest.fail(
            "No fernet.decrypt call site found in src/compadre/. "
            "The invariant expects exactly one, in keys/crypto.py."
        )

    # Exactly one hit in crypto.py
    if len(violations) > 1:
        pytest.fail(
            f"Multiple fernet.decrypt call sites found (expected 1 in "
            f"{_EXPECTED_FILE}):\n" + "\n".join(violations)
        )

    only = violations[0]
    if str(Path(_EXPECTED_FILE)) not in only:
        pytest.fail(
            f"fernet.decrypt found outside {_EXPECTED_FILE}:\n"
            f"  {only}\n"
            f"All decrypt calls must route through "
            f"compadre.keys.crypto.decrypt_tax_id()."
        )
