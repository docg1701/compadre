"""Billing provider contract and the manual-mode implementation.

The ABC is declared now so Story 2.1 (auth) and Story 3.3 (sweep) pick
up a stable interface. MVP has only ``ManualBillingProvider``; real PSP
adapters are a post-2.0.0 concern.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import date

from compadre.keys.models import ApiKeyRecord


class BillingProvider(ABC):
    """Contract shared by all billing-mode implementations."""

    @abstractmethod
    def is_payment_current(self, key: ApiKeyRecord, *, today: date) -> bool:
        """Return True when the key is currently paid / allowed to serve."""

    @abstractmethod
    def on_key_issued(self, key: ApiKeyRecord) -> None:
        """Hook invoked after a new key is persisted."""

    @abstractmethod
    def on_key_revoked(self, key: ApiKeyRecord) -> None:
        """Hook invoked after a key is revoked."""


class ManualBillingProvider(BillingProvider):
    """Operator-driven billing. No network calls, no PSP."""

    def is_payment_current(self, key: ApiKeyRecord, *, today: date) -> bool:
        if key.billing_mode == "complimentary":
            return True
        if key.billing_mode == "automated":
            # No PSP adapter exists in 2.0.0; an `automated` row was either
            # planted by a future-version write or by direct SQL edit. Refuse
            # rather than silently treating as `manual` — the manual paid_until
            # field is meaningless for automated billing.
            raise ValueError(
                "billing_mode='automated' is not supported in 2.0.0 "
                "(no PSP adapter); use 'manual' or 'complimentary'."
            )
        if key.paid_until is None:
            # Never marked paid — trial window. The ``issue-key`` panel
            # advertises a free runway up to ``first_charge_date`` and the
            # auth path honours it (allow-while-active SLA). Story 3.3's
            # sweep also leaves NULL rows alone (predicate is strict
            # ``paid_until < ?`` — NULL compares as NULL, not true). The
            # operator transitions out of trial via ``mark-paid``, which
            # writes a concrete date that this branch then evaluates.
            return True
        # Malformed paid_until raises ``ValueError``; the auth path catches
        # it, logs ERROR, and fails closed via ``ApiKeySuspendedError`` —
        # see ``compadre.keys.auth.verify_api_key_registry``.
        return date.fromisoformat(key.paid_until) >= today

    def on_key_issued(self, key: ApiKeyRecord) -> None:
        """No-op in MVP — hook exists for future PSP adapters."""
        del key

    def on_key_revoked(self, key: ApiKeyRecord) -> None:
        """No-op in MVP — hook exists for future PSP adapters."""
        del key


def first_charge_date(*, today: date, billing_day: int, tz: str) -> date:
    """Next occurrence of ``billing_day`` strictly after ``today``.

    Rule: if ``today.day == billing_day`` the next charge is next month's
    billing day (issuing on day 05 gives the rest of the month free; see
    Story 1.2 AC5). ``tz`` is accepted so the caller can document the
    timezone used; this function does no conversion (the caller passes
    ``today`` already resolved in ``tz``).
    """
    del tz  # documented but unused — see docstring
    if today.day < billing_day:
        return today.replace(day=billing_day)
    if today.month == 12:
        return date(today.year + 1, 1, billing_day)
    return date(today.year, today.month + 1, billing_day)


def days_until(d: date, *, from_date: date) -> int:
    """Whole-day delta from ``from_date`` to ``d``."""
    return (d - from_date).days
