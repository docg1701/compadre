"""Fixture — the canonical safe pattern.

Consumed by ``test_scan_ignores_compadre_error_raises``. A docstring
mentioning "HTTPException" by name (``ast.Constant`` string literal) is
structurally outside both scan rules, and ``raise VoiceNotFoundError(...)``
is a raise of a different class (``_terminal_function_name`` returns
``"VoiceNotFoundError"``, which is not ``"HTTPException"``). This fixture
pins both properties in one file: the scan walks Raise and ImportFrom
nodes only; docstrings are Constants; CompadreError subclass raises are not
HTTPException raises.
"""

from __future__ import annotations


class VoiceNotFoundError(Exception):
    """Local stub — production path uses ``src/compadre/exceptions.py``.

    The production ``VoiceNotFoundError`` subclasses ``CompadreError`` and
    sets ``status_code=404``. This fixture subclasses ``Exception`` only to
    keep the file self-contained (it is not imported at runtime). The scan
    does not care about the MRO — it inspects the raised expression's
    terminal name, which is ``"VoiceNotFoundError"`` either way.

    Also note: this docstring mentions ``HTTPException`` and ``from fastapi
    import HTTPException`` as literal strings. The scan must NOT flag them —
    they are ``ast.Constant`` nodes, not ``ast.ImportFrom`` or ``ast.Raise``.
    """


def resolve(voice: str) -> None:
    raise VoiceNotFoundError(f"Voice not found: {voice}")
