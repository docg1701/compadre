"""CPF/CNPJ validation (via `validate-docbr`) and Fernet encryption for tax IDs.

Validation delegates to the `validate-docbr` package, which maintains the
canonical Brazilian tax-ID algorithms and — since version 2.0 (2026-04) —
supports the alphanumeric CNPJ format defined by RFB IN 2.229/2024, effective
July 2026. The defaults `CPF(repeated_digits=False)` and `CNPJ()` reject
all-same-digit inputs and incorrect check digits.

Anti-enumeration (FR37, NFR16): `InvalidTaxIdError` NEVER receives the raw
input. The rejected value exists only in the caller's local scope and is
dropped when the exception propagates.

The dual primitives `encrypt_tax_id` / `decrypt_tax_id` are the only code
paths in this repository that touch `cryptography.fernet.Fernet`. Every
other module that needs a decrypted tax ID routes through `decrypt_tax_id`
so the "one storage-boundary module" invariant holds (architecture §Fernet
Encryption Pattern).
"""

from __future__ import annotations

import re

from cryptography.fernet import Fernet
from pydantic import SecretStr
from validate_docbr import CNPJ, CPF

from compadre.exceptions import InvalidTaxIdError

_MASK_CHARS = re.compile(r"[.\-/\s]")

_CPF = CPF()  # repeated_digits=False by default — rejects 111.111.111-11 etc.
_CNPJ = CNPJ()  # 2.0+ handles numeric and alphanumeric CNPJ.


def strip_tax_id(raw: str) -> str:
    """Return the canonical storage form of a CPF/CNPJ.

    Removes mask punctuation (`.`, `-`, `/`, whitespace) and uppercases the
    remainder. Uppercase is a no-op for numeric CPF/CNPJ but normalizes
    alphanumeric CNPJ so `12.abc.345/xy01-89` and `12.ABC.345/XY01-89` end
    up identical in `tax_id_encrypted`.
    """
    return _MASK_CHARS.sub("", raw).upper()


def validate_cpf(raw: str) -> None:
    """Validate a CPF (masked or unmasked). Raises `InvalidTaxIdError("cpf")`.

    Delegates to `validate_docbr.CPF.validate` against the canonical stripped
    form. The explicit length check is required because validate-docbr
    zero-pads inputs shorter than 11 digits to be tolerant of typed input —
    we want canonical 11-digit storage, so we reject shortforms upfront. The
    rejected input is never stored on the exception (FR37/NFR16
    anti-enumeration invariant).
    """
    canonical = strip_tax_id(raw)
    if len(canonical) != 11 or not _CPF.validate(canonical):
        raise InvalidTaxIdError("cpf")


def validate_cnpj(raw: str) -> None:
    """Validate a CNPJ (numeric or alphanumeric, masked or unmasked).

    Raises `InvalidTaxIdError("cnpj")` on failure. Validation runs against
    the canonical stripped form so the length check and checksum check agree
    on input shape. `validate-docbr` 2.0+ accepts the alphanumeric format
    defined in RFB IN 2.229/2024 (effective July 2026).
    """
    canonical = strip_tax_id(raw)
    if len(canonical) != 14 or not _CNPJ.validate(canonical):
        raise InvalidTaxIdError("cnpj")


def encrypt_tax_id(canonical: str, key: SecretStr) -> bytes:
    """Fernet-encrypt the stripped, uppercased tax ID. Returns ciphertext bytes."""
    fernet = Fernet(key.get_secret_value().encode())
    return fernet.encrypt(canonical.encode())


def compute_tax_id_lookup(canonical: str, key: SecretStr) -> bytes:
    """Return a deterministic 32-byte HMAC-SHA256 of ``canonical`` keyed by ``key``.

    Used by the ``customer.tax_id_lookup`` column (B2 / migration v3) to
    enforce CPF/CNPJ uniqueness without exposing plaintext or relying on
    randomised Fernet ciphertexts. Same input + same key → same output;
    different keys → different outputs (recomputed during
    ``rotate_encryption_keys``).

    The HMAC key is the same secret as ``encrypt_tax_id``: an attacker
    with the DB file alone cannot brute-force this column without also
    holding ``KEY_REGISTRY_ENCRYPTION_KEY`` — same defense-in-depth as
    ``tax_id_encrypted``. Callers MUST pass the canonical (stripped +
    uppercased) form via ``strip_tax_id`` so masked / lowercase variants
    of the same tax-ID hash to the same digest.
    """
    import hashlib
    import hmac

    return hmac.new(
        key.get_secret_value().encode(),
        canonical.encode(),
        hashlib.sha256,
    ).digest()


def decrypt_tax_id(ciphertext: bytes, key: SecretStr) -> str:
    """Fernet-decrypt a tax ID stored via ``encrypt_tax_id``.

    Returns the canonical stripped+uppercased form — the same shape
    ``encrypt_tax_id`` accepts. Callers that need to display the tax ID
    (e.g., ``export-customer``) format the canonical form via
    ``format_tax_id_display`` before emission; the decrypted string
    never leaves the caller's local scope otherwise.

    Raises ``cryptography.fernet.InvalidToken`` on ciphertext tampering,
    truncation, or key mismatch. Callers SHOULD NOT attempt to recover
    from ``InvalidToken`` by sniffing the bytes — propagate it as a
    database / configuration error (exit 5 / 6), never as a soft-fail
    that displays a placeholder tax ID.
    """
    fernet = Fernet(key.get_secret_value().encode())
    return fernet.decrypt(ciphertext).decode()
