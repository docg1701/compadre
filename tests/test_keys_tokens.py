"""Tests for compadre.keys.tokens — plaintext, hash, and prefix helpers."""

from __future__ import annotations

import re

from compadre.keys.tokens import (
    API_KEY_PREFIX,
    API_KEY_PREFIX_STORED_LEN,
    generate_api_key_plaintext,
    hash_api_key,
    key_prefix_from_plaintext,
)

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def test_generate_api_key_plaintext_shape_and_uniqueness() -> None:
    """Contract: ``cmpd_live_`` + 43 URL-safe-b64 chars; first stored char
    is alphanumeric (regression: a leading ``-`` / ``_`` makes Click parse
    the token as an option group, so CLI tests would flake). 1000 samples
    are unique."""
    samples = {generate_api_key_plaintext() for _ in range(1000)}
    assert len(samples) == 1000
    for plaintext in samples:
        assert plaintext.startswith(API_KEY_PREFIX)
        assert len(plaintext) == 53  # 10 prefix + 43 b64
        body = plaintext.removeprefix(API_KEY_PREFIX)
        assert re.fullmatch(r"[A-Za-z0-9_-]{43}", body)
        assert plaintext[len(API_KEY_PREFIX)].isalnum()  # CLI-safety


def test_hash_api_key_is_deterministic_sha256_hex() -> None:
    """SHA-256 hex (64 lowercase hex), deterministic, distinct for distinct
    inputs. Pinned vs a known constant guards against algorithm refactor."""
    digest = hash_api_key("x")
    assert isinstance(digest, str)  # not bytes — must use .hexdigest()
    assert _HEX64.fullmatch(digest)
    assert digest == "2d711642b726b04401627ca9fbac32f5c8530fb1903cc4db02258717921a4881"
    assert hash_api_key("x") == digest
    assert hash_api_key("x") != hash_api_key("y")


def test_key_prefix_from_plaintext_eight_chars_after_fixed_prefix() -> None:
    plaintext = "cmpd_live_ABCDEFGHijKLMNOPqrstuvwxyz0123456789ab_-"
    assert key_prefix_from_plaintext(plaintext) == "ABCDEFGH"
    assert API_KEY_PREFIX_STORED_LEN == 8
    # Generator output also satisfies the slice length.
    assert len(key_prefix_from_plaintext(generate_api_key_plaintext())) == 8
