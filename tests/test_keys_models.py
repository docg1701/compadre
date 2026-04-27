"""Tests for ``compadre.keys.models`` — only custom validators / business rules.

Lean by design (test-suite review 2026-04-25): per-class
``test_is_frozen`` / ``test_has_slots`` / ``test_round_trip_fields``
collapsed away — those are pydantic / dataclass framework guarantees
the framework's own test suite covers. What survives are the rules
that ARE business logic and would silently break a real user flow:

* ``ApiKeyCreate`` rejects malformed ``customer_id`` (defense against
  upstream typos that would later FK-orphan).
* ``AuthPayload`` does not carry PII fields — defense-in-depth against a
  future leaky cache.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from compadre.keys.db import generate_customer_id
from compadre.keys.models import ApiKeyCreate, AuthPayload


def test_api_key_create_rejects_malformed_customer_id() -> None:
    """Defense against upstream typos that would otherwise FK-orphan."""
    for bad in ("cust_nope", "customer_01jabc", "cust_UPPERCASE" + "a" * 18):
        with pytest.raises(ValidationError):
            ApiKeyCreate(
                customer_id=bad,
                rate_limit_rpm=60,
                billing_mode="manual",
                price_monthly=149.0,
            )

    # Generated IDs must round-trip.
    cid = generate_customer_id()
    accepted = ApiKeyCreate(
        customer_id=cid,
        rate_limit_rpm=60,
        billing_mode="manual",
        price_monthly=149.0,
    )
    assert accepted.customer_id == cid


def test_auth_payload_carries_no_pii() -> None:
    """Defense-in-depth: a payload that ends up cached MUST NOT carry
    name/email/ciphertext/key_hash/plaintext attributes. A future
    refactor that wires extras into the AuthPayload trips this guard.
    """
    payload = AuthPayload(
        customer_id="cust_01abc",
        key_id="key_01abc",
        status="active",
        billing_mode="manual",
        rate_limit_rpm=60,
        paid_until="2099-12-31",
    )
    assert not hasattr(payload, "__dict__")  # slots / frozen
    for forbidden in ("name", "email", "tax_id_encrypted", "key_hash", "plaintext"):
        assert not hasattr(payload, forbidden)
    with pytest.raises((AttributeError, TypeError)):
        payload.name = "leak"  # type: ignore[attr-defined]
