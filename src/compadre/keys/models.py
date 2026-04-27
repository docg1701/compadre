"""Data models for the key registry.

`CustomerCreate` validates CLI/API input. `CustomerRecord` is the immutable
row representation used by the DB layer. Literals `TaxIdKind`, `CustomerStatus`,
`BillingMode` are the authoritative value sets for the schema's CHECK
constraints.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, EmailStr, Field

TaxIdKind = Literal["cpf", "cnpj"]
CustomerStatus = Literal["active", "suspended", "revoked"]
BillingMode = Literal["manual", "complimentary", "automated"]


class CustomerCreate(BaseModel):
    """Validated input for creating a customer.

    `tax_id_digits` is digits-only post-`strip_tax_id` and already passed
    `validate_cpf` / `validate_cnpj`. Stored separately from `tax_id_kind`
    so callers don't re-validate inside the model layer.
    """

    name: str = Field(min_length=1, max_length=200)
    email: EmailStr
    tax_id_kind: TaxIdKind
    tax_id_digits: str = Field(
        min_length=11,
        max_length=14,
        pattern=r"^[0-9A-Z]+$",
    )
    notes: str = ""


@dataclass(frozen=True, slots=True)
class CustomerRecord:
    """Immutable customer row as persisted in SQLite."""

    id: str
    name: str
    email: str
    tax_id_kind: TaxIdKind
    tax_id_encrypted: bytes
    notes: str
    registered_at: datetime
    deleted_at: datetime | None


class ApiKeyCreate(BaseModel):
    """Validated input for issuing an API key (CLI-side)."""

    customer_id: str = Field(pattern=r"^cust_[0-9a-z]{26}$")
    rate_limit_rpm: int = Field(gt=0)
    billing_mode: BillingMode = "manual"
    price_monthly: float = Field(ge=0.0)
    notes: str = ""


@dataclass(frozen=True, slots=True)
class ApiKeyRecord:
    """Immutable api_key row as persisted in SQLite."""

    id: str
    customer_id: str
    key_hash: str
    key_prefix: str
    rate_limit_rpm: int
    status: CustomerStatus
    billing_mode: BillingMode
    price_monthly: float
    paid_until: str | None
    created_at: datetime
    last_used_at: datetime | None
    notes: str


@dataclass(frozen=True, slots=True)
class CustomerListRow:
    """One row of the ``list-customers`` result set — immutable display payload.

    Timestamps stay as raw SQLite TEXT (ISO 8601 UTC) rather than round-tripping
    through ``datetime``; the renderer slices the first 10 characters to display
    ``YYYY-MM-DD``. Avoiding the conversion removes a class of tz / fractional
    second bugs on the hot path of listings.
    """

    id: str
    name: str
    email: str
    tax_id_kind: TaxIdKind
    key_count: int
    registered_at: str
    deleted_at: str | None
    notes: str


@dataclass(frozen=True, slots=True)
class ApiKeyListRow:
    """One row of the ``list-keys`` result set — immutable display payload.

    ``customer_name`` is joined in so the renderer doesn't issue a second
    query per row. ``key_hash`` is deliberately NOT carried — list output
    never exposes the hash (FR37 anti-enumeration).

    ``notes`` is included so the Story 3.2 ``revenue`` report can list a
    complimentary key's bundled context without issuing a second query
    per row. The Story 1.3 ``list-keys`` table output does not render
    this column (width budget); the ``--format json`` path does emit it.
    """

    id: str
    customer_id: str
    customer_name: str
    key_prefix: str
    status: CustomerStatus
    billing_mode: BillingMode
    rate_limit_rpm: int
    price_monthly: float
    paid_until: str | None
    created_at: str
    last_used_at: str | None
    notes: str


@dataclass(frozen=True, slots=True)
class CustomerDetailRow:
    """One row of the ``show <cust_id>`` result — immutable display payload.

    ``key_count`` is a LEFT JOIN aggregate. ``tax_id_encrypted`` is deliberately
    NOT included — the detail view only surfaces ``tax_id_kind``.
    """

    id: str
    name: str
    email: str
    tax_id_kind: TaxIdKind
    key_count: int
    registered_at: str
    notes: str


@dataclass(frozen=True, slots=True)
class KeyDetailRow:
    """One row of the ``show <key-prefix>`` result — immutable display payload.

    ``age_days`` is computed by the caller (uses ``_resolve_today_in_tz`` seam
    so tests can freeze the clock). ``customer_name`` is joined in so the
    renderer doesn't need a second query. ``key_hash`` is deliberately NOT
    included.
    """

    id: str
    customer_id: str
    customer_name: str
    key_prefix: str
    status: CustomerStatus
    billing_mode: BillingMode
    rate_limit_rpm: int
    price_monthly: float
    paid_until: str | None
    created_at: str
    last_used_at: str | None
    age_days: int
    notes: str


@dataclass(frozen=True, slots=True)
class AuditEventRow:
    """One row of the ``audit_log`` slice served by ``--audit-tail``."""

    id: int
    occurred_at: str
    actor: str
    action: str
    target_type: str | None
    target_id: str | None
    details_json: str | None


@dataclass(frozen=True, slots=True)
class IssuedKey:
    """One-shot display payload for ``issue-key`` / ``rotate-key``.

    Carries the plaintext from the command body to the renderer in a
    typed container so the plaintext string is not passed around as a
    bare ``str`` — reduces the chance of accidental logging.

    ``first_charge_date`` is an ISO date string for manual keys, None for
    complimentary. ``first_charge_days_free`` is the pre-computed whole-day
    delta from today-in-billing-TZ to that date; the command resolves it
    before calling the renderer so the display is decoupled from any clock.
    """

    plaintext: str
    record: ApiKeyRecord
    customer_name: str
    first_charge_date: str | None
    first_charge_days_free: int | None


@dataclass(frozen=True, slots=True)
class Last24hStats:
    """Per-key counter snapshot row rendered by ``show <key-prefix> --audit-tail``.

    Populated by aggregating every per-replica ``{db}.counters.*.json``
    file (Story 2.3). Callers upstream pass ``None`` to
    ``render_last_24h_stats`` when no replica reports this key.

    Counter fields are summed across replicas, except ``peak_rpm`` which
    is the max observed in any one replica and ``last_activity_at`` which
    is the most recent timestamp anywhere. ``snapshot_age_seconds`` is the
    age of the FRESHEST contributing snapshot; ``process_uptime_seconds``
    is the MIN across contributing replicas (the most conservative window
    the data has been accumulating). ``replicas`` counts how many replicas
    reported this key so the renderer can surface the multi-replica nature
    of the aggregate.
    """

    requests_total: int
    auth_success: int
    rate_limit_429: int
    peak_rpm: int
    avg_rpm_last_60s: int
    last_activity_at: str | None
    snapshot_age_seconds: int
    process_uptime_seconds: int
    replicas: int


@dataclass(frozen=True, slots=True)
class RevenueReport:
    """Aggregated monthly revenue payload for ``revenue_cmd`` (Story 3.2).

    ``mrr`` counts only **paid** manual-mode revenue for the reporting
    month. ``projected`` counts paid + pending (paid + "still billable").
    Complimentary rows are carried separately and NEVER summed into any
    total — FR17 hard requirement. ``month_iso`` is the stable
    ``YYYY-MM`` key JSON consumers parse; ``month_label`` is the human
    label (English per the project's language policy).
    """

    month_label: str
    month_iso: str
    paid_count: int
    pending_count: int
    suspended_count: int
    complimentary_count: int
    mrr: float
    projected: float
    next_charge_date: str
    complimentary_rows: tuple[ApiKeyListRow, ...]


@dataclass(frozen=True, slots=True)
class AuthPayload:
    """Minimal authorization payload cached per bearer-token hash.

    Populated by the auth path, read by the rate-limit resolver and any
    downstream middleware that needs the per-key context. No personal
    data — no name / email / tax_id / plaintext / hash. The ``key_id``
    field carries the stable ``key_*`` ULID so counters and logging can
    reference the key across rotations (the 8-char ``key_prefix`` changes
    on rotate; ``key_id`` does not).
    """

    customer_id: str
    key_id: str
    status: CustomerStatus
    billing_mode: BillingMode
    rate_limit_rpm: int
    paid_until: str | None
