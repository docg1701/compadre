"""Tests for ``compadre.keys.crypto`` — CPF/CNPJ + Fernet primitives.

Lean by design (test-suite review 2026-04-25): per-CPF/CNPJ parametrize
collapsed (validate-docbr tests its own); kept the security-critical
contracts: encryption round-trip, ciphertext non-determinism, decrypt
fails on tamper / wrong-key, and FR37/NFR16 rejected-value
non-leakage.
"""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet, InvalidToken
from pydantic import SecretStr

from compadre.exceptions import InvalidTaxIdError
from compadre.keys.crypto import (
    decrypt_tax_id,
    encrypt_tax_id,
    strip_tax_id,
    validate_cnpj,
    validate_cpf,
)


def test_strip_tax_id_removes_punctuation_uppercases_letters() -> None:
    """Canonicalize: dots/dashes/slashes/whitespace removed; letters
    upper-cased so alphanumeric CNPJ comparisons stay deterministic."""
    assert strip_tax_id("123.456.789-09") == "12345678909"
    assert strip_tax_id("12.345.678/0001-95") == "12345678000195"
    assert strip_tax_id("  12abc345xy0189  ") == "12ABC345XY0189"


def test_validate_cpf_accepts_valid_rejects_bad_checksum() -> None:
    validate_cpf("12345678909")  # valid checksum — no raise
    validate_cpf("123.456.789-09")  # masked form accepted

    for bad in ("12345678900", "11111111111", "1234567890"):
        with pytest.raises(InvalidTaxIdError):
            validate_cpf(bad)


def test_validate_cnpj_accepts_valid_rejects_bad_checksum() -> None:
    validate_cnpj("12345678000195")
    validate_cnpj("12.345.678/0001-95")
    for bad in ("12345678000100", "11111111111111", "12345"):
        with pytest.raises(InvalidTaxIdError):
            validate_cnpj(bad)


def test_encrypt_tax_id_round_trips_and_is_nondeterministic() -> None:
    """Round-trip + IV-randomness: the same plaintext under the same
    key produces different ciphertexts (Fernet IV randomness is
    security-critical)."""
    key = SecretStr(Fernet.generate_key().decode())

    for canonical in ("12345678909", "12345678000195", "12ABC345XY0189"):
        ciphertext = encrypt_tax_id(canonical, key)
        assert ciphertext.startswith(b"gAAAAA")
        assert decrypt_tax_id(ciphertext, key) == canonical

    # Non-determinism (IV randomness).
    c1 = encrypt_tax_id("12345678909", key)
    c2 = encrypt_tax_id("12345678909", key)
    assert c1 != c2


def test_decrypt_tax_id_rejects_tamper_wrong_key_and_empty() -> None:
    """Defense: any ciphertext modification (1 bit), wrong key, or
    empty bytes must raise InvalidToken (NOT silently return garbage)."""
    key_a = SecretStr(Fernet.generate_key().decode())
    key_b = SecretStr(Fernet.generate_key().decode())
    ciphertext = encrypt_tax_id("12345678909", key_a)

    tampered = ciphertext[:-1] + bytes([ciphertext[-1] ^ 0x01])
    with pytest.raises(InvalidToken):
        decrypt_tax_id(tampered, key_a)
    with pytest.raises(InvalidToken):
        decrypt_tax_id(ciphertext, key_b)
    with pytest.raises(InvalidToken):
        decrypt_tax_id(b"", key_a)


def test_invalid_tax_id_error_does_not_leak_rejected_value() -> None:
    """FR37/NFR16: a rejected CPF/CNPJ value must NOT survive on the
    exception (no .value attribute, no digits in message/hint)."""
    rejected = "11111111111"  # all-same-digit valid-shape CPF
    try:
        validate_cpf(rejected)
    except InvalidTaxIdError as exc:
        for attr in ("value", "raw", "input", "digits", "tax_id"):
            assert not hasattr(exc, attr)
        assert rejected not in str(exc)
        assert rejected not in exc.message
        assert rejected not in exc.hint
        # Message + hint contain no digits at all.
        assert not any(ch.isdigit() for ch in exc.message)
        assert not any(ch.isdigit() for ch in exc.hint)
