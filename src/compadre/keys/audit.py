"""Append-only audit log writer.

`log_event` writes a single `audit_log` row using the caller-supplied
connection. Callers MUST open the surrounding transaction (e.g., via
`insert_customer`) so the business mutation and its audit row either both
commit or both roll back.

FR30 defense-in-depth: `details` must not carry personal data. A blocklist
raises `ValueError` if a forbidden key slips through — never `assert`, which
Python strips under `-O` / `PYTHONOPTIMIZE=1`. The check is case-insensitive
and recurses into nested `Mapping` AND `Sequence` values so a forbidden key
buried in `{"items": [{"name": "..."}]}` still fails. Symmetric with
`filter_forbidden_keys` (read-side defense-in-depth).
"""

from __future__ import annotations

import getpass
import json
import os
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime

_FORBIDDEN_DETAIL_KEYS = frozenset(
    {
        "cpf",
        "cnpj",
        "tax_id",
        "name",
        "email",
        "key_plaintext",
        "authorization",
    }
)


def _assert_no_forbidden_value(value: object) -> None:
    """Recurse into mappings and sequences, asserting no forbidden keys."""
    if isinstance(value, Mapping):
        _assert_no_forbidden_keys(value)
    # str / bytes are Sequences but never carry nested keys; explicit guard
    # so the recursion does not iterate characters.
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        for item in value:
            _assert_no_forbidden_value(item)


def _assert_no_forbidden_keys(payload: Mapping[str, object]) -> None:
    for key, value in payload.items():
        if key.lower() in _FORBIDDEN_DETAIL_KEYS:
            raise ValueError(
                f"audit details contain forbidden personal data key: {key!r}"
            )
        _assert_no_forbidden_value(value)


def _filter_forbidden_value(value: object) -> object:
    """Recurse into mappings and sequences, scrubbing forbidden keys."""
    if isinstance(value, Mapping):
        return filter_forbidden_keys(value)
    # str / bytes are Sequences but never carry nested keys; explicit guard.
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_filter_forbidden_value(item) for item in value]
    return value


def filter_forbidden_keys(payload: Mapping[str, object]) -> dict[str, object]:
    """Recursively strip any ``_FORBIDDEN_DETAIL_KEYS`` from ``payload``.

    The audit-write path's ``_assert_no_forbidden_keys`` guarantees stored
    rows cannot carry forbidden keys, so this filter is pure defense-in-depth
    for read paths (Story 5.1 ``export-customer --include-audit``) — if a
    corrupt or out-of-band INSERT ever sneaks a PII key through, the export
    still won't echo it. Recurses into nested ``Mapping`` AND ``Sequence``
    values so a forbidden key buried in ``[{"name": "..."}]`` is also
    stripped. Returns a fresh dict; never mutates the input.
    """
    out: dict[str, object] = {}
    for key, value in payload.items():
        if key.lower() in _FORBIDDEN_DETAIL_KEYS:
            continue
        out[key] = _filter_forbidden_value(value)
    return out


def _utcnow_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


_ACTOR_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


def _sanitize_actor(username: str) -> str:
    """Replace any character outside [A-Za-z0-9._-] with `?` for log safety.

    Defends against utmp poisoning that would otherwise inject control bytes
    or audit-log-row separators (`|`, newline) into the actor column. Cap at
    64 chars so a pathological username cannot bloat audit-row sizes.
    """
    cleaned = _ACTOR_SAFE_RE.sub("?", username)[:64]
    return cleaned


def get_cli_actor() -> str:
    """Return `cli:<username>` with fallbacks to getpass then `cli:unknown`."""
    try:
        username = os.getlogin()
    except OSError:
        username = ""
    if not username:
        try:
            username = getpass.getuser()
        except (OSError, KeyError):
            username = ""
    return f"cli:{_sanitize_actor(username) or 'unknown'}"


def log_event(
    conn: sqlite3.Connection,
    *,
    actor: str,
    action: str,
    target_type: str | None,
    target_id: str | None,
    details: dict[str, object] | None = None,
    occurred_at: str | None = None,
) -> None:
    """Append one row to `audit_log` using the caller's transaction.

    `details` is serialized with `sort_keys=True` for deterministic test
    assertions. Forbidden keys (case-insensitive, including nested dicts)
    raise `ValueError` — callers should never pass them.

    `occurred_at` lets callers pin the timestamp string rather than letting
    the helper sample the clock. The suspension sweep (Story 3.3) pins it so
    the returned `completed_at` has byte-identity with the summary row's
    `occurred_at` (AC3). All other call sites pass `None` and accept the
    helper's `_utcnow_iso()` default.
    """
    details_json: str | None
    if details is None:
        details_json = None
    else:
        _assert_no_forbidden_keys(details)
        details_json = json.dumps(details, ensure_ascii=False, sort_keys=True)

    conn.execute(
        "INSERT INTO audit_log ("
        "occurred_at, actor, action, target_type, target_id, details_json"
        ") VALUES (?, ?, ?, ?, ?, ?)",
        (
            occurred_at if occurred_at is not None else _utcnow_iso(),
            actor,
            action,
            target_type,
            target_id,
            details_json,
        ),
    )
