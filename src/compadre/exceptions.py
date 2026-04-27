"""Custom exception hierarchy for Compadre."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from starlette.requests import Request
from starlette.responses import JSONResponse

from compadre.logging import get_logger

if TYPE_CHECKING:
    from fastapi import FastAPI

_logger = get_logger("request")


class CompadreError(Exception):
    """Base exception for all Compadre errors."""

    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        error_type: str,
        hint: str,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.error_type = error_type
        self.hint = hint


class VoiceNotFoundError(CompadreError):
    """Raised when a requested voice is not available."""

    def __init__(self, voice: str, available: list[str]) -> None:
        super().__init__(
            message=f"Voice '{voice}' not found",
            status_code=404,
            error_type="not_found_error",
            hint=(
                f"Available voices: {available}."
                " Use 'kokoro/<name>' for Kokoro voices (.npy files or built-ins)"
                " or 'qwen/<name>' for Qwen3 voices (.wav files in /voices/)."
            ),
        )
        self.voice = voice
        self.available = available


class InvalidRequestError(CompadreError):
    """Raised when request parameters are invalid."""

    def __init__(self, message: str, hint: str) -> None:
        super().__init__(
            message=message,
            status_code=400,
            error_type="invalid_request_error",
            hint=hint,
        )


class GenerationError(CompadreError):
    """Raised when TTS generation fails."""

    def __init__(self, message: str) -> None:
        super().__init__(
            message=message,
            status_code=500,
            error_type="server_error",
            hint=(
                "Check server logs for the full traceback."
                " The TTS model may need restarting."
            ),
        )


class AuthenticationError(CompadreError):
    """Raised when authentication fails."""

    def __init__(self, message: str = "Invalid or missing API key") -> None:
        super().__init__(
            message=message,
            status_code=401,
            error_type="authentication_error",
            hint=(
                "Ensure your Authorization header uses the format"
                " 'Bearer <your-api-key>'."
            ),
        )


class ApiKeyNotFoundError(CompadreError):
    """Raised when the bearer token's SHA-256 hash does not match any row.

    Envelope is deliberately identical to ``AuthenticationError`` for
    enumeration-resistance (FR26 / PRD L662). The distinction exists only so
    server-side logging / future telemetry can discriminate.
    """

    def __init__(self) -> None:
        super().__init__(
            message="Invalid or missing API key",
            status_code=401,
            error_type="authentication_error",
            hint=(
                "Ensure your Authorization header uses the format"
                " 'Bearer <your-api-key>'."
            ),
        )


class ApiKeySuspendedError(CompadreError):
    """Raised when the key was found but its status is 'suspended' or its
    manual-mode ``paid_until`` is in the past.

    ``operator_contact_hint`` is interpolated ONCE at construction time so the
    handler never re-formats attacker-controlled strings.
    """

    def __init__(self, operator_contact_hint: str) -> None:
        # B11 — strip trailing space when ``operator_contact_hint`` is
        # empty (the 2.0.0 default). The previous f-string left
        # "Your API key has been suspended. " with a dangling space that
        # made monitoring grep patterns fragile.
        suffix = f" {operator_contact_hint}" if operator_contact_hint else ""
        super().__init__(
            message="Your API key has been suspended",
            status_code=401,
            error_type="authentication_error",
            hint=f"Your API key has been suspended.{suffix}",
        )


class ApiKeyRevokedError(CompadreError):
    """Raised when the key was found but its status is 'revoked'."""

    def __init__(self, operator_contact_hint: str) -> None:
        # B11 — same trailing-space cleanup as ``ApiKeySuspendedError``.
        suffix = f" {operator_contact_hint}" if operator_contact_hint else ""
        super().__init__(
            message="Your API key has been revoked",
            status_code=401,
            error_type="authentication_error",
            hint=f"Your API key has been revoked.{suffix}",
        )


class RateLimitError(CompadreError):
    """Raised when a client exceeds the rate limit.

    Carries the ``limits`` window snapshot so ``compadre_error_handler`` can
    emit the ``X-RateLimit-*`` headers on the 429 response — matching the
    headers emitted on the allowed-request path by ``check_rate_limit``.
    """

    def __init__(
        self,
        retry_after: int,
        *,
        limit: int,
        remaining: int,
        reset: int,
    ) -> None:
        super().__init__(
            message="Rate limit exceeded",
            status_code=429,
            error_type="rate_limit_error",
            hint=f"Too many requests. Try again in {retry_after} seconds.",
        )
        self.retry_after = retry_after
        self.limit = limit
        self.remaining = remaining
        self.reset = reset


class InvalidTaxIdError(CompadreError):
    """Raised when a CPF/CNPJ fails checksum verification.

    Deliberately does not accept the raw input value — anti-enumeration
    (FR37, NFR16). Error text is static per tax_id_kind.
    """

    def __init__(self, kind: Literal["cpf", "cnpj"]) -> None:
        label = kind.upper()
        super().__init__(
            message=f"Invalid {label}: checksum verification failed",
            status_code=400,
            error_type="invalid_request_error",
            hint=f"Verify the {label} digits and try again.",
        )
        self.kind = kind


class DuplicateTaxIdError(CompadreError):
    """Raised when ``create-customer`` / ``correct-tax-id`` would store a
    CPF/CNPJ that is already registered against an active customer (B2).

    Anti-enumeration: the raw value is NEVER carried on the exception.
    The message and hint are static; the operator already typed the
    value so echoing it back is redundant and would leak through a
    forensic ``stderr`` capture.
    """

    def __init__(self, kind: Literal["cpf", "cnpj"]) -> None:
        label = kind.upper()
        super().__init__(
            message=f"{label} already registered to another active customer",
            status_code=409,
            error_type="invalid_request_error",
            hint=(
                "Run 'compadre-keys list-customers' to find the existing "
                "customer. To re-register a previously hard-deleted "
                f"{label}, the tombstone must be in place "
                "(``tax_id_lookup`` is cleared on hard-delete)."
            ),
        )
        self.kind = kind


class KeyRegistryError(CompadreError):
    """Raised when the key registry SQLite database is unreachable or corrupt."""

    def __init__(
        self, message: str = "Key registry database unreachable or corrupt"
    ) -> None:
        super().__init__(
            message=message,
            status_code=500,
            error_type="server_error",
            hint="Check KEY_REGISTRY_DB_PATH exists, is readable, and has mode 0600.",
        )


class ServiceUnavailableError(CompadreError):
    """Raised when the service is not ready to handle requests."""

    def __init__(
        self, message: str = "Service is warming up", *, retry_seconds: int = 30
    ) -> None:
        super().__init__(
            message=message,
            status_code=503,
            error_type="service_unavailable",
            hint=(
                "The service is still loading models."
                f" Retry in {retry_seconds} seconds."
            ),
        )
        self.retry_seconds = retry_seconds


async def compadre_error_handler(request: Request, exc: CompadreError) -> JSONResponse:
    """Handle CompadreError subclasses with structured JSON response."""
    headers: dict[str, str] | None = None
    if isinstance(exc, RateLimitError):
        headers = {
            "Retry-After": str(exc.retry_after),
            "X-RateLimit-Limit": str(exc.limit),
            "X-RateLimit-Remaining": str(exc.remaining),
            "X-RateLimit-Reset": str(exc.reset),
        }
    elif isinstance(exc, ServiceUnavailableError):
        headers = {"Retry-After": str(exc.retry_seconds)}
    return JSONResponse(
        status_code=exc.status_code,
        content={
            "error": {
                "message": exc.message,
                "type": exc.error_type,
                "hint": exc.hint,
            }
        },
        headers=headers,
    )


async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    """Handle unexpected exceptions with generic 500 response."""
    _logger.error("Unhandled exception during request processing", exc_info=exc)
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": "An internal server error occurred",
                "type": "server_error",
                "hint": "Check server logs for the full traceback.",
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    """Register centralized exception handlers on the FastAPI app."""
    app.add_exception_handler(CompadreError, compadre_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(Exception, unhandled_error_handler)
