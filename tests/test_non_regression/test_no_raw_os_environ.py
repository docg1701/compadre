"""FR51 non-regression — no raw ``os.environ`` reads outside config (Story 4.5g).

Architecture contract: ``config.py`` is the sole pydantic-settings consumer.
All other modules MUST obtain settings from the ``Settings`` model (injected
via dependency or module-level singleton), NEVER from ``os.environ[...]``,
``os.getenv(...)``, or ``from os import environ``.

Justified exemption: ``src/compadre/download.py`` sets ``HF_HOME`` for
``huggingface_hub.snapshot_download()`` which reads ``os.environ`` directly
(the library does not accept a ``cache_dir`` parameter at the API level).
This exemption is documented via ``# scan-allow[no-raw-os-environ]: <reason>``.

This test walks every ``.py`` under ``src/compadre/`` and asserts zero
violating AST nodes outside the config.py and download.py allowlist.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from tests.test_non_regression._scan_common import allowlisted_lines, safe_parse

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
_SRC_ROOT = _PROJECT_ROOT / "src" / "compadre"
_SCAN_LABEL: str = "no-raw-os-environ"

# Files that are exempt from the no-raw-os.environ check by design:
# - config.py: pydantic-settings sole consumer
# - download.py: justified exemption for hf_home (scan-allow comment present)
_EXEMPT_FILES: frozenset[str] = frozenset({"config.py", "download.py"})


@dataclass(frozen=True)
class _Violation:
    path: Path
    lineno: int
    kind: str  # "os.environ[...]", "os.getenv(...)", "from os import environ"
    source: str

    def render(self) -> str:
        rel = self.path.relative_to(_PROJECT_ROOT)
        return f"{rel}:{self.lineno}: raw {self.kind} — {self.source.strip()}"


class _OsEnvironVisitor(ast.NodeVisitor):
    """AST visitor that collects raw os.environ / os.getenv / from-os-import-environ."""

    def __init__(self, path: Path, source: str, allowlisted: set[int]) -> None:
        self.path = path
        self._source_lines = source.splitlines()
        self._allowlisted = allowlisted
        self.violations: list[_Violation] = []

    def _is_allowlisted(self, node: ast.AST) -> bool:
        return node.lineno in self._allowlisted

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.module == "os":
            for alias in node.names:
                if alias.name == "environ" and not self._is_allowlisted(node):
                    self.violations.append(
                        _Violation(
                            path=self.path,
                            lineno=node.lineno,
                            kind="from os import environ",
                            source=self._source_lines[node.lineno - 1],
                        )
                    )
        self.generic_visit(node)

    def visit_Subscript(self, node: ast.Subscript) -> None:
        # os.environ["KEY"] or os.environ.get("KEY")
        if (
            isinstance(node.value, ast.Attribute)
            and isinstance(node.value.value, ast.Name)
            and node.value.value.id == "os"
            and node.value.attr == "environ"
            and not self._is_allowlisted(node)
        ):
            self.violations.append(
                _Violation(
                    path=self.path,
                    lineno=node.lineno,
                    kind="os.environ[...]",
                    source=self._source_lines[node.lineno - 1],
                )
            )
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # os.getenv("KEY") or os.environ.get("KEY")
        if (
            isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "os"
            and node.func.attr == "getenv"
            and not self._is_allowlisted(node)
        ):
            self.violations.append(
                _Violation(
                    path=self.path,
                    lineno=node.lineno,
                    kind="os.getenv(...)",
                    source=self._source_lines[node.lineno - 1],
                )
            )
        self.generic_visit(node)


def _scan_file(path: Path) -> list[_Violation]:
    """Scan a single .py file for raw os.environ usage."""
    source = path.read_text(encoding="utf-8")
    tree = safe_parse(path)
    allowlisted = allowlisted_lines(source, label=_SCAN_LABEL)
    visitor = _OsEnvironVisitor(path, source, allowlisted)
    visitor.visit(tree)
    return visitor.violations


@pytest.mark.non_regression
def test_no_raw_os_environ_outside_config() -> None:
    """Walk src/compadre/ and assert zero raw os.environ outside exempt files."""
    all_violations: list[_Violation] = []

    for py_file in sorted(_SRC_ROOT.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        rel = str(py_file.relative_to(_SRC_ROOT))
        if any(rel == exempt or rel.endswith(f"/{exempt}") for exempt in _EXEMPT_FILES):
            continue
        all_violations.extend(_scan_file(py_file))

    if all_violations:
        lines = [v.render() for v in all_violations]
        pytest.fail(
            f"Found {len(all_violations)} raw os.environ usage(s) outside "
            f"config.py/download.py:\n" + "\n".join(lines) + "\n\n"
            "Fix: read from compadre.config.Settings instead. "
            "If justified (like hf_home in download.py), add "
            "`# scan-allow[no-raw-os-environ]: <reason>` on the line."
        )
