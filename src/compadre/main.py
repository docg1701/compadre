"""Compadre FastAPI application entry point."""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.responses import Response

from compadre import __version__
from compadre.api import api_router
from compadre.config import get_settings
from compadre.counters import (
    RequestCounters,
    counters_snapshot_path,
    default_replica_id,
)
from compadre.download import ensure_whisper_model
from compadre.exceptions import (
    CompadreError,
    KeyRegistryError,
    register_exception_handlers,
)
from compadre.health import build_health_response, build_minimal_health_response
from compadre.keys.auth import verify_api_key
from compadre.keys.cache import AuthCache
from compadre.keys.db import _apply_migrations, get_connection
from compadre.logging import get_logger, setup_logging
from compadre.rate_limit import (
    RateLimiter,
    check_health_per_ip_rate_limit,
    get_client_ip,
)
from compadre.stt import WhisperEngine

logger = get_logger("startup")


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Application lifespan: startup sequence."""
    settings = get_settings()

    # Initialize state early (health endpoint needs these during startup)
    app.state.ready = False
    app.state.queue_lock = asyncio.Lock()
    app.state.queue_depth = 0
    app.state.start_time = time.monotonic()
    app.state.settings = settings
    app.state.engine = None

    # Phase 1: Load config (already done via get_settings)
    logger.info("Configuration loaded")

    # Phase 2: Configure structured logging
    setup_logging()
    logger.info("Structured logging configured")

    # Phase 2a: Key-registry bootstrap
    logger.info("Opening key registry at %s", settings.key_registry_db_path)
    app.state.registry_db = get_connection(settings.key_registry_db_path)
    _apply_migrations(app.state.registry_db)
    # B2: backfill the v3 tax_id_lookup column on first-startup-after-upgrade
    # so the partial UNIQUE INDEX takes effect against pre-existing rows.
    # Idempotent and free for new deployments (no rows to backfill).
    from compadre.keys.db import backfill_tax_id_lookup_if_needed

    backfilled = backfill_tax_id_lookup_if_needed(
        app.state.registry_db,
        encryption_key=settings.key_registry_encryption_key,
    )
    if backfilled:
        logger.info("Backfilled tax_id_lookup for %d customer row(s)", backfilled)
    sentinel_path = Path(f"{settings.key_registry_db_path}.invalidate")
    # F16 — cross-mount sentinel probe. Touch the sentinel as the API
    # process and read it back to confirm the parent directory is
    # writable AND that the read-after-write is visible. A non-bind-mount
    # copy of the DB directory (or a CLI process running on the host
    # against an in-container DB) would silently fail this read-back,
    # leaving revoked keys authenticatable for up to the 60s cache TTL.
    # Warn loudly at startup so the operator can spot the misconfig
    # before live traffic exposes it.
    try:
        sentinel_path.touch(exist_ok=True)
        try:
            probed_mtime = sentinel_path.stat().st_mtime_ns
        except OSError:
            probed_mtime = 0
        if probed_mtime == 0:
            logger.warning(
                "Auth-cache sentinel probe FAILED — sentinel touch did not "
                "produce a readable mtime at %s. Cross-mount invalidation "
                "WILL NOT work; revoked keys may stay authenticatable for "
                "up to the 60s cache TTL. Verify the registry directory is "
                "a single shared filesystem between the API and CLI.",
                sentinel_path,
            )
    except OSError as exc:
        logger.warning(
            "Auth-cache sentinel probe FAILED — could not touch %s: %s. "
            "Cross-mount invalidation will not work; the CLI's revocations "
            "may stay invisible until the next cache TTL boundary. Verify "
            "the registry directory is writable by the API process.",
            sentinel_path,
            exc,
        )
    app.state.auth_cache = AuthCache(invalidation_sentinel=sentinel_path)
    app.state.rate_limiter = RateLimiter.build()
    # Story 2.3 — per-key request counters + periodic snapshot writer. The
    # snapshot file is the CLI's window into the in-memory counters; 15s is
    # the tension point between "fresh enough for operator forensics" and
    # "cheap enough to fsync without impact". ``start_snapshot_writer``
    # requires a running loop, which ``lifespan`` is. The snapshot path
    # carries a per-replica segment so multi-replica deployments produce
    # one file per replica (CLI aggregates via glob).
    replica_id = default_replica_id()
    counters_path = counters_snapshot_path(
        settings.key_registry_db_path, replica_id=replica_id
    )
    app.state.request_counters = RequestCounters(
        snapshot_path=counters_path,
        snapshot_interval_seconds=15,
    )
    app.state.request_counters.start_snapshot_writer(
        auth_cache_snapshot=app.state.auth_cache.snapshot,
    )
    logger.info(
        "Key registry ready; auth cache initialised (maxsize=%d, ttl=%ds, "
        "invalidation_sentinel=%s); per-key rate-limiter initialised; "
        "request counters + snapshot writer scheduled "
        "(path=%s, replica_id=%s, interval=%ds)",
        app.state.auth_cache.maxsize,
        app.state.auth_cache.ttl,
        sentinel_path,
        counters_path,
        replica_id,
        app.state.request_counters.snapshot_interval_seconds,
    )

    # Phase 2.5: Ensure Whisper model is downloaded (non-blocking)
    logger.info("Checking Whisper model files...")
    await asyncio.to_thread(
        ensure_whisper_model,
        model_size=settings.whisper_model_size,
        cache_dir=settings.hf_home,
    )
    logger.info("Model check complete")

    try:
        # Phase 3: Load Whisper engine
        logger.info("Loading Whisper STT engine...")
        engine = WhisperEngine()
        engine.load_model(
            model_size=settings.whisper_model_size,
            device="cpu",
            compute_type=settings.whisper_compute_type,
            cpu_threads=settings.whisper_cpu_threads,
        )

        # Phase 4: Warm up
        logger.info("Warming up Whisper engine...")
        engine.warm_up()
    except Exception:
        logger.critical("Startup failed during engine initialization", exc_info=True)
        raise

    # Phase 5: Ready
    app.state.engine = engine
    app.state.ready = True

    logger.info("Service ready on http://0.0.0.0:%d", settings.port)

    try:
        yield
    finally:
        logger.info("Shutting down...")
        rc = getattr(app.state, "request_counters", None)
        if rc is not None:
            # Best-effort final snapshot + task cancel. A hung writer (e.g.
            # filesystem full) would otherwise delay container exit; the
            # ``stop_snapshot_writer`` internals swallow all write failures.
            await rc.stop_snapshot_writer()
        db = getattr(app.state, "registry_db", None)
        if db is not None:
            db.close()


_GENERIC_VALIDATION_HINT = (
    "Expected multipart/form-data: file=@audio.opus, response_format=text, language=pt"
)


def _format_validation_error(exc: RequestValidationError) -> tuple[str, str]:
    """Translate the first pydantic error into a (message, hint) pair (B20).

    Operators reading the API response shouldn't need to read the raw
    pydantic error envelope to know whether the input was empty, oversized,
    or wrong shape. This helper inspects the first error's ``type`` (the
    pydantic v2 stable identifier — ``string_too_short``,
    ``string_too_long``, ``missing``, ``string_type``, etc.) and emits a
    field-specific message.

    Falls back to the legacy generic message when the error shape is
    unfamiliar (e.g. the ``model_validator`` raises a ``ValueError`` with
    a custom message — those already carry a useful ``msg`` we surface
    verbatim).
    """
    errors = exc.errors()
    if not errors:
        return "Invalid request body", _GENERIC_VALIDATION_HINT
    first = errors[0]
    err_type = first.get("type", "")
    loc = first.get("loc") or ()
    field = next((str(p) for p in loc if p != "body"), "request body")
    raw_msg = str(first.get("msg") or "").strip()

    if err_type == "string_too_short":
        return (
            f"Field '{field}' must not be empty",
            f"Provide a non-empty {field}.",
        )
    if err_type == "string_too_long":
        ctx = first.get("ctx") or {}
        max_len = ctx.get("max_length")
        max_clause = f" (max {max_len} chars)" if max_len is not None else ""
        return (
            f"Field '{field}' exceeds the allowed length{max_clause}",
            f"Trim {field}{max_clause} or fewer.",
        )
    if err_type == "missing":
        return (
            f"Field '{field}' is required",
            _GENERIC_VALIDATION_HINT,
        )
    if err_type in {"string_type", "int_type", "float_type", "bool_type"}:
        expected = err_type.removesuffix("_type")
        return (
            f"Field '{field}' must be a {expected}",
            _GENERIC_VALIDATION_HINT,
        )
    if err_type == "enum":
        ctx = first.get("ctx") or {}
        expected = ctx.get("expected", "")
        return (
            f"Field '{field}' has an invalid value",
            f"Allowed values: {expected}." if expected else _GENERIC_VALIDATION_HINT,
        )
    if err_type in {
        "greater_than",
        "greater_than_equal",
        "less_than",
        "less_than_equal",
    }:
        ctx = first.get("ctx") or {}
        bound_label = {
            "greater_than": "greater than",
            "greater_than_equal": "≥",
            "less_than": "less than",
            "less_than_equal": "≤",
        }[err_type]
        bound_value = ctx.get(
            {
                "greater_than": "gt",
                "greater_than_equal": "ge",
                "less_than": "lt",
                "less_than_equal": "le",
            }[err_type]
        )
        clause = f" {bound_label} {bound_value}" if bound_value is not None else ""
        return (
            f"Field '{field}' is out of range{clause}",
            (
                f"Provide a value{clause} for '{field}'."
                if bound_value is not None
                else f"Provide a valid value for '{field}'."
            ),
        )
    if err_type in {"dict_type", "model_attributes_type"}:
        return (
            "Request body must be a JSON object",
            _GENERIC_VALIDATION_HINT,
        )
    if err_type == "value_error":
        # ``model_validator`` ValueErrors — already carry a tailored msg.
        # Surface verbatim.
        return raw_msg or "Invalid request body", _GENERIC_VALIDATION_HINT
    if err_type == "json_invalid":
        return ("Malformed JSON body", _GENERIC_VALIDATION_HINT)
    return "Invalid request body", _GENERIC_VALIDATION_HINT


async def validation_error_handler(
    request: Request, exc: RequestValidationError
) -> JSONResponse:
    """Convert FastAPI 422 validation errors to 400 with consistent error format.

    B20 — distinguishes empty input / oversized input / missing field /
    wrong shape via ``_format_validation_error``. Falls back to the legacy
    generic ``Invalid request body`` for unknown error types.
    """
    message, hint = _format_validation_error(exc)
    return JSONResponse(
        status_code=400,
        content={
            "error": {
                "message": message,
                "type": "invalid_request_error",
                "hint": hint,
            }
        },
    )


_REFLECTION_MAX_LEN = 200


def _sanitize_for_reflection(value: str) -> str:
    """Strip control bytes and cap length before echoing into a JSON body.

    The 404/405 handlers reflect the request method + path back to the caller
    for diagnostic clarity. Without sanitisation, an attacker-controlled URL
    with control bytes (CR, ANSI escape, NUL, other C0 controls) lands in
    log-aggregator JSON fields and either misrenders or, on TTY-rendering
    aggregators, forges terminal formatting. JSON-encoding makes the body
    XSS-safe; this guard makes it log-aggregator-safe and operator-safe.
    """
    cleaned = "".join(ch for ch in value if ch == "\t" or ord(ch) >= 0x20)
    if len(cleaned) > _REFLECTION_MAX_LEN:
        cleaned = cleaned[:_REFLECTION_MAX_LEN] + "…"
    return cleaned


def _not_found_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert Starlette HTTPException(404) to our standard error format.

    Without this handler, FastAPI returns ``{"detail": "Not Found"}`` which
    breaks the consistent ``{"error": {message, type, hint}}`` envelope used
    everywhere else in the API.
    """
    method = _sanitize_for_reflection(request.method)
    path = _sanitize_for_reflection(request.url.path)
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "message": f"Not found: {method} {path}",
                "type": "not_found_error",
                "hint": (
                    "Check the URL. The Compadre API surface is"
                    " /v1/audio/transcriptions and /health."
                ),
            }
        },
    )


def _method_not_allowed_handler(request: Request, exc: Exception) -> JSONResponse:
    """Convert Starlette HTTPException(405) to our standard error format.

    Without this handler, FastAPI returns ``{"detail": "Method Not Allowed"}``
    which breaks the consistent ``{"error": {message, type, hint}}`` envelope
    used everywhere else. The Allow header from the original exception is
    preserved when present.
    """
    headers: dict[str, str] | None = None
    exc_headers = getattr(exc, "headers", None)
    if isinstance(exc_headers, dict) and "Allow" in exc_headers:
        headers = {"Allow": exc_headers["Allow"]}
    method = _sanitize_for_reflection(request.method)
    path = _sanitize_for_reflection(request.url.path)
    return JSONResponse(
        status_code=405,
        content={
            "error": {
                "message": f"Method {method} not allowed on {path}",
                "type": "method_not_allowed_error",
                "hint": "Use POST for /v1/audio/transcriptions and GET for /health.",
            }
        },
        headers=headers,
    )


def create_app() -> FastAPI:
    """Create and configure the FastAPI application."""
    app = FastAPI(
        title="Compadre",
        version=__version__,
        lifespan=lifespan,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        redirect_slashes=False,
    )
    access_logger = get_logger("access")

    @app.middleware("http")
    async def access_log_middleware(request: Request, call_next: Any) -> Response:
        start = time.monotonic()
        response: Response = await call_next(request)
        duration = time.monotonic() - start
        client_ip = get_client_ip(request)
        access_logger.info(
            "%s %s %s %d %.1fs",
            client_ip,
            request.method,
            request.url.path,
            response.status_code,
            duration,
        )
        # Apply X-RateLimit-* headers stashed by ``check_rate_limit``.
        # A dedicated middleware (not dependency-injected Response headers)
        # is the reliable channel because routes returning their own
        # Response object replace the injected one, losing its headers.
        extra = getattr(request.state, "rate_limit_headers", None)
        if extra:
            for name, value in extra.items():
                response.headers[name] = value
        return response

    register_exception_handlers(app)
    app.add_exception_handler(RequestValidationError, validation_error_handler)  # type: ignore[arg-type]
    app.add_exception_handler(404, _not_found_handler)
    app.add_exception_handler(405, _method_not_allowed_handler)
    app.include_router(api_router)

    async def _try_auth(request: Request) -> bool:
        """Return True if the request carries a valid API key.

        Normal auth denials (missing header, unknown key, suspended,
        revoked) surface as ``CompadreError`` subclasses and are expected
        for any unauthenticated ``/health`` probe — caller treats them as
        "fall back to minimal envelope", no log line. A ``KeyRegistryError``
        is NOT a normal denial: it means the registry DB is unreachable
        or corrupt, and every authenticated request is failing closed.
        Log it so a silent ``/health 200 minimal`` doesn't mask a broken
        auth path. Anything else also gets an ERROR-level log.
        """
        try:
            await verify_api_key(
                request,
                authorization=request.headers.get("authorization", ""),
            )
        except KeyRegistryError:
            logger.error(
                "Key registry error during /health auth probe — "
                "authenticated traffic is failing closed",
                exc_info=True,
            )
            return False
        except CompadreError:
            return False
        except Exception:
            logger.error(
                "Unexpected exception during /health auth probe",
                exc_info=True,
            )
            return False
        return True

    @app.get("/health")
    async def health_endpoint(request: Request) -> JSONResponse:
        # F14 — per-IP rate-limit on /health (60/min/IP) to bound the
        # shape-valid-random DoS surface. Runs BEFORE _try_auth so a
        # token-spray attack against the SQLite-serialized lookup_key_auth
        # path is throttled at the IP layer regardless of bearer validity.
        await check_health_per_ip_rate_limit(request)
        authenticated = await _try_auth(request)

        if not getattr(request.app.state, "ready", False):
            engine = getattr(request.app.state, "engine", None)
            if engine is None:
                minimal: dict[str, Any] = {
                    "status": "warming_up",
                }
                if authenticated:
                    minimal.update(
                        {
                            "models": ["large-v3-turbo"],
                            "engines": {},
                            "queue_depth": 0,
                            "uptime_seconds": 0,
                            "memory_mb": 0,
                            "version": __version__,
                        }
                    )
                return JSONResponse(content=minimal, status_code=503)

        if authenticated:
            body, status_code = build_health_response(
                engine=request.app.state.engine,
                ready=request.app.state.ready,
                start_time=request.app.state.start_time,
                queue_depth=request.app.state.queue_depth,
            )
        else:
            body, status_code = build_minimal_health_response(
                engine=request.app.state.engine,
                ready=request.app.state.ready,
            )
        return JSONResponse(content=body, status_code=status_code)

    return app
