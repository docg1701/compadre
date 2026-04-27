"""Plaintext key generation, hashing, and prefix extraction.

Lives in its own module (not db.py) because Story 2.1's auth path will
import `hash_api_key` to hash inbound Bearer tokens. CLI (this story)
imports `generate_api_key_plaintext` + `key_prefix_from_plaintext` +
`hash_api_key`; the auth path will import only `hash_api_key`. Keeping
the module dep-free (stdlib only) prevents circular imports when
`auth.py` is added.
"""

from __future__ import annotations

import hashlib
import secrets

API_KEY_PREFIX = "cmpd_live_"
# First 8 chars AFTER the fixed `cmpd_live_` prefix — the unique part.
API_KEY_PREFIX_STORED_LEN = 8


def generate_api_key_plaintext() -> str:
    """Return a fresh plaintext API key: ``cmpd_live_`` + 43-char token.

    ``secrets.token_urlsafe(32)`` returns 43 URL-safe base64 characters
    without padding, so the full plaintext is 53 chars.

    The first character after ``cmpd_live_`` (i.e. position 10, which is
    also ``key_prefix_from_plaintext`` byte 0) is constrained to be
    alphanumeric. URL-safe base64 includes ``-`` and ``_``; if the stored
    prefix starts with either, every CLI invocation that takes the prefix
    as a positional arg (``show``, ``suspend``, ``mark-paid``, …) trips
    Click's option parser and exits 2. Reroll until the first stored-prefix
    char is ``[A-Za-z0-9]``. Entropy cost is negligible (~1 bit on the
    leading char only; the remaining 42 chars preserve full base64 entropy)
    and average iterations is 32/30 ≈ 1.07.
    """
    while True:
        candidate = f"{API_KEY_PREFIX}{secrets.token_urlsafe(32)}"
        if candidate[len(API_KEY_PREFIX)].isalnum():
            return candidate


def hash_api_key(plaintext: str) -> str:
    """Return lowercase hex SHA-256 digest of the plaintext.

    Always ``.hexdigest()`` (never raw bytes) — the DB stores this in a
    ``TEXT UNIQUE`` column and Story 2.1's auth path must hash inbound
    Bearer tokens identically.
    """
    return hashlib.sha256(plaintext.encode("ascii")).hexdigest()


def key_prefix_from_plaintext(plaintext: str) -> str:
    """Return the 8-char public prefix after the fixed ``cmpd_live_`` tag."""
    start = len(API_KEY_PREFIX)
    return plaintext[start : start + API_KEY_PREFIX_STORED_LEN]
