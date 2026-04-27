"""Tests for compadre.keys.billing — first-charge math + ManualBillingProvider."""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from compadre.keys.billing import (
    BillingProvider,
    ManualBillingProvider,
    days_until,
    first_charge_date,
)
from compadre.keys.models import ApiKeyRecord

_TZ = "America/Sao_Paulo"


def _record(
    *, billing_mode: str = "manual", paid_until: str | None = None
) -> ApiKeyRecord:
    return ApiKeyRecord(
        id="key_01abc",
        customer_id="cust_01j8xr3e4c5m7kn9p2qt8vwyz0",
        key_hash="a" * 64,
        key_prefix="ABCDEFGH",
        rate_limit_rpm=60,
        status="active",
        billing_mode=billing_mode,  # type: ignore[arg-type]
        price_monthly=149.0 if billing_mode == "manual" else 0.0,
        paid_until=paid_until,
        created_at=datetime(2026, 4, 16, 12, 0, 0, tzinfo=UTC),
        last_used_at=None,
        notes="",
    )


@pytest.mark.parametrize(
    ("today", "billing_day", "expected"),
    [
        (date(2026, 4, 13), 5, date(2026, 5, 5)),  # mid-month → next month
        (date(2026, 4, 5), 5, date(2026, 5, 5)),  # same-day → next month (AC5)
        (date(2026, 12, 31), 5, date(2027, 1, 5)),  # year rollover
        (date(2026, 1, 4), 5, date(2026, 1, 5)),  # before billing day → same month
    ],
)
def test_first_charge_date_table(today: date, billing_day: int, expected: date) -> None:
    assert first_charge_date(today=today, billing_day=billing_day, tz=_TZ) == expected


def test_days_until_positive_and_zero() -> None:
    assert days_until(date(2026, 5, 5), from_date=date(2026, 4, 13)) == 22
    assert days_until(date(2026, 4, 13), from_date=date(2026, 4, 13)) == 0


class TestManualBillingProvider:
    def test_payment_currency_branches(self) -> None:
        """Complimentary always current; manual depends on paid_until vs today.

        ``paid_until is None`` is the trial window: the ``issue-key`` panel
        advertises a free runway and the auth path honours it. A regression
        here would 401 every freshly-issued manual key (B10, 2026-04-26).
        """
        provider = ManualBillingProvider()
        today = date(2026, 4, 15)
        assert provider.is_payment_current(
            _record(billing_mode="complimentary"), today=today
        )
        assert provider.is_payment_current(_record(paid_until=None), today=today)
        assert provider.is_payment_current(
            _record(paid_until="2026-04-15"), today=today
        )
        assert not provider.is_payment_current(
            _record(paid_until="2026-04-14"), today=today
        )

    def test_lifecycle_hooks_are_noop(self) -> None:
        provider = ManualBillingProvider()
        rec = _record()
        assert provider.on_key_issued(rec) is None
        assert provider.on_key_revoked(rec) is None

    def test_provider_contract(self) -> None:
        """ABC cannot be instantiated; ManualBillingProvider satisfies it."""
        assert issubclass(ManualBillingProvider, BillingProvider)
        with pytest.raises(TypeError):
            BillingProvider()  # type: ignore[abstract]
