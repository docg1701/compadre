"""Registry-backed API-key authentication.

``verify_api_key`` is the FastAPI dependency wired onto
``POST /v1/audio/speech`` — it parses the bearer, shape-checks it
against the ``cmpd_live_<43-char>`` contract, and delegates to
``verify_api_key_registry`` which runs the DB + cache lookup. Sets
``request.state.auth_payload`` on success; raises a ``CompadreError``
subclass on any denial outcome.

The module's only runtime I/O is a single indexed SELECT against the key
registry on cache-miss. Zero external deps — the timing-equalisation
dummy compare is ``hmac.compare_digest`` from stdlib.
"""

from __future__ import annotations

import datetime as _dt
import hmac
import re
import sqlite3
from datetime import date
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import Header, Request

from compadre.config import RegistrySettings
from compadre.exceptions import (
    ApiKeyNotFoundError,
    ApiKeyRevokedError,
    ApiKeySuspendedError,
    AuthenticationError,
    KeyRegistryError,
)
from compadre.keys.billing import ManualBillingProvider
from compadre.keys.cache import AuthCache
from compadre.keys.db import lookup_key_auth
from compadre.keys.models import ApiKeyRecord, AuthPayload
from compadre.keys.tokens import API_KEY_PREFIX, hash_api_key
from compadre.logging import get_logger

if TYPE_CHECKING:
    from compadre.counters import RequestCounters

_logger = get_logger("keys.auth")

# Pre-computed dummy hash for timing-equalisation. Constant per process.
_DUMMY_HASH = hash_api_key("__dummy_for_timing_equalisation__")

# Valid plaintext shape: ``cmpd_live_`` + 43 URL-safe base64 chars =
# 53 chars total. Any token outside this shape cannot match a registry
# row, so ``verify_api_key`` short-circuits to a 401 before touching the
# DB — this blocks unknown-bearer spray attacks (every probe would
# otherwise serialize on the single sqlite3 connection). The regex
# enforces the URL-safe base64 charset on the suffix so Unicode or
# whitespace-padded tokens are rejected before any cache or DB lookup.
_VALID_TOKEN_RE = re.compile(rf"\A{re.escape(API_KEY_PREFIX)}[A-Za-z0-9_-]{{43}}\Z")

# Shared billing provider instance. MVP has exactly one implementation.
_billing = ManualBillingProvider()


def _today_in_billing_tz(settings: RegistrySettings) -> date:
    """Return today's date resolved in ``settings.billing_timezone``.

    Extracted as a single-caller seam so tests can monkey-patch
    ``compadre.keys.auth._today_in_billing_tz`` without touching
    ``date.today`` globally. Mirrors the CLI's ``_resolve_today_in_tz``
    pattern (which lives under ``keys/cli/`` and cannot be imported here
    per the architecture's import-boundary rule).
    """
    return _dt.datetime.now(ZoneInfo(settings.billing_timezone)).date()


async def verify_api_key(
    request: Request,
    authorization: str = Header(default=""),
) -> None:
    """FastAPI dependency — registry-mode bearer auth.

    Parses the ``Authorization: Bearer <token>`` header, shape-checks the
    token against the ``cmpd_live_<43-char>`` contract, then delegates to
    ``verify_api_key_registry`` (which carries the actual DB + cache
    logic and is directly callable by tests without HTTP framing).

    The shape check short-circuits to ``ApiKeyNotFoundError`` before any
    DB read when the bearer can't possibly match a stored plaintext. This
    blocks unknown-bearer spray attacks from serialising on the single
    sqlite3 connection (critical on ``/health``, which is not
    rate-limited at the app layer). The ``cmpd_live_`` prefix is public
    information (documented in ``issue-key`` output), so the timing
    distinction between "bad shape" and "good shape, unknown" leaks
    nothing an attacker couldn't already read from the docs.
    """
    # RFC 6750 §2.1: the "Bearer" scheme is case-insensitive. Tokens are
    # not — they must be compared byte-for-byte against the registry hash.
    scheme_end = authorization.find(" ")
    if scheme_end != 6 or authorization[:6].lower() != "bearer":
        raise AuthenticationError("Missing or malformed Authorization header")
    token = authorization[7:]
    if not _VALID_TOKEN_RE.match(token):
        raise ApiKeyNotFoundError()
    await verify_api_key_registry(request, token=token)


async def verify_api_key_registry(request: Request, token: str) -> None:
    """Registry-mode auth core. Sets ``request.state.auth_payload`` on pass.

    Never writes to ``audit_log``. Never updates ``last_used_at``. Fails
    closed on any SQLite error. Kept as a separately-callable function
    (not inlined into ``verify_api_key``) so tests can exercise the full
    auth logic without constructing an HTTP ``Authorization`` header.

    F18 — timing equalisation between cache hit and miss. The cache hit
    path runs the same indexed DB lookup the miss path does (against the
    dummy hash, which never matches), so the total wallclock cost of
    both paths is dominated by the same SELECT. A wallclock observer
    cannot distinguish "this key is cached (active recently)" from
    "this key was looked up from the registry just now" — closes the
    cache-residency oracle. The cache still avoids the cost of actually
    parsing and constructing an ``AuthPayload`` on hit; the dummy SELECT
    only retrieves zero rows from a btree-indexed seek that is the
    fastest a SQLite SELECT can be.
    """
    settings: RegistrySettings = request.app.state.settings
    cache: AuthCache = request.app.state.auth_cache
    conn = request.app.state.registry_db

    try:
        token_hash = hash_api_key(token)
    except UnicodeEncodeError:
        # Non-ASCII tokens cannot match any stored hash (issue-key never
        # produces non-ASCII plaintext). Run the dummy compare so the
        # rejection cost matches a real DB-miss, then reject with the
        # enumeration-resistant 401 envelope.
        hmac.compare_digest(_DUMMY_HASH, _DUMMY_HASH)
        raise ApiKeyNotFoundError() from None
    payload = cache.get(token_hash)

    if payload is not None:
        # F18 — equalise: incur the same indexed-SELECT cost the miss
        # path pays. ``_DUMMY_HASH`` is shaped like a real hash but
        # never matches a stored row. Equalisation must never break
        # the auth path — a SELECT error on the dummy hash would only
        # matter if the registry is unreachable, which the miss-path
        # SELECT would also surface; on hit, the cached payload is
        # authoritative.
        import contextlib

        with contextlib.suppress(sqlite3.Error):
            lookup_key_auth(conn, _DUMMY_HASH)

    if payload is None:
        try:
            payload = lookup_key_auth(conn, token_hash)
        except sqlite3.Error as exc:
            _logger.error("Key registry database unreachable or corrupt", exc_info=exc)
            raise KeyRegistryError() from exc
        if payload is None:
            # Timing-equalisation: match the compare_digest cost of a cache-hit.
            hmac.compare_digest(token_hash, _DUMMY_HASH)
            raise ApiKeyNotFoundError()
        cache.set(token_hash, payload)

    if payload.status == "suspended":
        raise ApiKeySuspendedError(settings.operator_contact_hint)
    if payload.status == "revoked":
        raise ApiKeyRevokedError(settings.operator_contact_hint)

    record = _payload_to_record(payload)
    try:
        paid = _billing.is_payment_current(record, today=_today_in_billing_tz(settings))
    except ValueError as exc:
        _logger.error(
            "Malformed paid_until in registry for key_id=%s; failing closed",
            payload.key_id,
            exc_info=exc,
        )
        raise ApiKeySuspendedError(settings.operator_contact_hint) from exc
    except ZoneInfoNotFoundError as exc:
        # Invalid ``BILLING_TIMEZONE`` or missing tzdata wheel on the host.
        # Operator-side misconfiguration; fail closed with a 500 so monitors
        # actually surface it (vs. leaking a 500 trace per request).
        _logger.error(
            "Cannot resolve billing_timezone=%r; failing closed",
            settings.billing_timezone,
            exc_info=exc,
        )
        raise KeyRegistryError() from exc
    if not paid:
        raise ApiKeySuspendedError(settings.operator_contact_hint)

    request.state.auth_payload = payload

    # Story 2.3 — register the request against the per-key counters. The
    # counters live on ``app.state.request_counters`` and are optional at
    # this boundary so test fixtures that skip lifespan (``tests/conftest``
    # builds the app directly) keep passing. Both increments run on the
    # same success path; any denial above raises before reaching this
    # point, so ``requests_total`` is "requests that successfully
    # authenticated" by construction. A later 429 bumps ``rate_limit_429``
    # without re-incrementing ``requests_total``.
    counters: RequestCounters | None = getattr(
        request.app.state, "request_counters", None
    )
    if counters is not None:
        counters.incr_request(payload.key_id)
        counters.incr_auth_success(payload.key_id)


def _payload_to_record(payload: AuthPayload) -> ApiKeyRecord:
    """Adapt ``AuthPayload`` to ``ApiKeyRecord`` for ``ManualBillingProvider``.

    Only ``billing_mode`` and ``paid_until`` are consumed by the provider;
    other fields carry placeholder values — this record never persists and
    never reaches a DB writer.
    """
    return ApiKeyRecord(
        id=payload.key_id,
        customer_id=payload.customer_id,
        key_hash="",
        key_prefix="",
        rate_limit_rpm=payload.rate_limit_rpm,
        status=payload.status,
        billing_mode=payload.billing_mode,
        price_monthly=0.0,
        paid_until=payload.paid_until,
        created_at=_dt.datetime(1970, 1, 1, tzinfo=_dt.UTC),
        last_used_at=None,
        notes="",
    )
