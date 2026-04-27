"""Shared helpers for extracting user-facing messages from ValidationError.

Leading-underscore module name signals "CLI-internal, not a public API" —
same convention used by ``_reset_for_tests`` in ``output.py``.
"""

from __future__ import annotations

from pydantic import ValidationError


def first_error_msg(exc: ValidationError) -> str:
    """Return the first sub-error message with the offending field, or a fallback.

    Example outputs:
        "api_key: Field required"
        "key_registry_db_path: Path does not exist"
    Falls back to the bare message if no field location is available.
    """
    errors = exc.errors()
    if not errors:
        return "Check the input values."
    first = errors[0]
    msg = str(first.get("msg", "Check the input values."))
    loc = first.get("loc", ())
    if loc:
        field = ".".join(str(part) for part in loc)
        return f"{field}: {msg}"
    return msg


def is_missing_encryption_key(exc: ValidationError) -> bool:
    """Return True when the ValidationError is specifically about the encryption key.

    Matches either the custom model-level validator message or a direct
    ``key_registry_encryption_key`` field error.
    """
    for err in exc.errors():
        loc = err.get("loc", ())
        if loc and loc[0] == "key_registry_encryption_key":
            return True
        msg = str(err.get("msg", ""))
        if "KEY_REGISTRY_ENCRYPTION_KEY" in msg:
            return True
    return False
