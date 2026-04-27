"""Tests for the CompadreError hierarchy + handler.

Lean by design (test-suite review 2026-04-25): per-class
status_code/error_type/hint readback was tautology. What survives:
anti-enumeration envelope identity, format-string safety, handler
JSON shape.
"""

from __future__ import annotations

import json

from starlette.requests import Request

from compadre.exceptions import (
    ApiKeyNotFoundError,
    ApiKeyRevokedError,
    ApiKeySuspendedError,
    AuthenticationError,
    InvalidRequestError,
    InvalidTaxIdError,
    compadre_error_handler,
)


def _mock_request() -> Request:
    return Request(
        {"type": "http", "method": "POST", "path": "/v1/audio/transcriptions"}
    )


def test_api_key_not_found_envelope_is_byte_identical_to_authentication_error() -> None:
    """FR26 anti-enumeration: a DB-miss MUST NOT be distinguishable
    from a malformed bearer at the HTTP layer."""
    not_found = ApiKeyNotFoundError()
    legacy = AuthenticationError()
    for attr in ("message", "error_type", "hint", "status_code"):
        assert getattr(not_found, attr) == getattr(legacy, attr)


def test_suspended_revoked_hints_interpolate_operator_contact_safely() -> None:
    """The hint format has ``{operator_contact_hint}`` as the ONLY
    placeholder — operator-supplied braces (e.g. ``Contact {admin}!``)
    must remain literal, not get interpreted by str.format()."""
    for cls in (ApiKeySuspendedError, ApiKeyRevokedError):
        err = cls(operator_contact_hint="Contact {admin}!")
        assert err.hint.endswith("Contact {admin}!")
        assert "{admin}" in err.hint
        assert "{operator_contact_hint}" not in err.hint


def test_invalid_request_and_tax_id_errors() -> None:
    """InvalidRequestError and InvalidTaxIdError carry expected shape."""
    req = InvalidRequestError(message="too long", hint="shorten it")
    assert req.status_code == 400
    assert "too long" in req.message

    tax = InvalidTaxIdError("cpf")
    assert tax.status_code == 400
    assert "cpf" in tax.message.lower()


async def test_compadre_error_handler_maps_subclasses_to_correct_envelope() -> None:
    """The handler maps any CompadreError to ``{"error": {message, type, hint}}``
    with the exception's status_code. Two cases cover the dispatch path."""
    for exc, expected_status, expected_type in (
        (
            InvalidRequestError(message="too long", hint="shorten it"),
            400,
            "invalid_request_error",
        ),
        (
            AuthenticationError(),
            401,
            "authentication_error",
        ),
    ):
        resp = await compadre_error_handler(_mock_request(), exc)
        assert resp.status_code == expected_status
        body = json.loads(
            resp.body if isinstance(resp.body, bytes) else bytes(resp.body)
        )
        error = body["error"]
        assert isinstance(error, dict)
        assert {"message", "type", "hint"} <= error.keys()
        assert error["type"] == expected_type
