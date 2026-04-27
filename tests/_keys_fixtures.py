"""Shared seeding helpers for key-registry integration tests.

Duplication of ``_seed_key`` across multiple test modules would drift;
consolidate once in this helper and import from both ``test_keys_auth.py``
and ``test_api.py``.
"""

from __future__ import annotations

import sqlite3

from compadre.keys.db import generate_customer_id, generate_key_id
from compadre.keys.tokens import generate_api_key_plaintext, hash_api_key


def seed_customer_and_key(
    conn: sqlite3.Connection,
    *,
    customer_name: str = "Helena Ribeiro",
    customer_email: str = "helena@clinica.com",
    status: str = "active",
    billing_mode: str = "manual",
    rate_limit_rpm: int = 60,
    paid_until: str | None = "2099-12-31",
    price_monthly: float | None = None,
    plaintext: str | None = None,
) -> tuple[str, str, str]:
    """Seed one customer + one api_key row.

    Returns ``(plaintext, key_id, customer_id)``. Commits the insert so the
    FastAPI test client sees the rows on the next connection lookup.
    """
    plaintext = plaintext or generate_api_key_plaintext()
    cid = generate_customer_id()
    kid = generate_key_id()
    if price_monthly is None:
        price_monthly = 0.0 if billing_mode == "complimentary" else 149.0

    conn.execute(
        "INSERT INTO customer ("
        "id, name, email, tax_id_kind, tax_id_encrypted, notes, "
        "registered_at, deleted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            cid,
            customer_name,
            customer_email,
            "cpf",
            b"ciphertext",
            "",
            "2026-04-16T12:00:00Z",
            None,
        ),
    )
    conn.execute(
        "INSERT INTO api_key ("
        "id, customer_id, key_hash, key_prefix, rate_limit_rpm, status, "
        "billing_mode, price_monthly, paid_until, created_at, last_used_at, "
        "notes) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            kid,
            cid,
            hash_api_key(plaintext),
            plaintext[len("cmpd_live_") : len("cmpd_live_") + 8],
            rate_limit_rpm,
            status,
            billing_mode,
            price_monthly,
            paid_until,
            "2026-04-16T12:00:00Z",
            None,
            "",
        ),
    )
    conn.commit()
    return plaintext, kid, cid
