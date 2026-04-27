"""Transcription endpoint route handler for Compadre."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from tempfile import NamedTemporaryFile

from fastapi import APIRouter, Depends, File, Form, Request, Response, UploadFile
from fastapi.responses import JSONResponse

from compadre.exceptions import (
    CompadreError,
    InvalidRequestError,
    ServiceUnavailableError,
)
from compadre.formatters import (
    format_json,
    format_srt,
    format_text,
    format_verbose_json,
    format_vtt,
)
from compadre.keys.auth import verify_api_key
from compadre.logging import get_logger
from compadre.preprocessing import decode_audio_to_wav, get_audio_duration
from compadre.rate_limit import check_rate_limit, check_transcription_per_ip_rate_limit
from compadre.schemas import ResponseFormat

logger = get_logger("request")

api_router = APIRouter()


async def verify_ready(request: Request) -> None:
    """Reject requests during startup warm-up."""
    if not getattr(request.app.state, "ready", False):
        raise ServiceUnavailableError(
            message="Service is warming up, engine not yet loaded",
            retry_seconds=30,
        )


@api_router.post(
    "/v1/audio/transcriptions",
    dependencies=[
        Depends(verify_ready),
        Depends(check_transcription_per_ip_rate_limit),
        Depends(verify_api_key),
        Depends(check_rate_limit),
    ],
)
async def transcribe_audio(
    request: Request,
    file: UploadFile = File(...),  # noqa: B008
    model: str = Form("whisper-large-v3-turbo"),
    language: str | None = Form(None),
    response_format: str = Form("text"),
    temperature: float = Form(0.0),
) -> Response:
    """Transcribe audio to text. OpenAI Whisper API compatible."""
    settings = request.app.state.settings
    engine = request.app.state.engine
    queue_lock = request.app.state.queue_lock

    # Validate response_format
    try:
        fmt = ResponseFormat(response_format)
    except ValueError as exc:
        raise InvalidRequestError(
            message=f"Unsupported response_format: '{response_format}'",
            hint="Supported: text, json, verbose_json, srt, vtt.",
        ) from exc

    # Validate uploaded file
    if file.size is not None and file.size > settings.max_upload_size_mb * 1024 * 1024:
        raise InvalidRequestError(
            message=f"File too large ({file.size / (1024 * 1024):.1f} MB)",
            hint=f"Maximum upload size is {settings.max_upload_size_mb} MB.",
        )

    # Stream upload to temp file to avoid loading entire file into memory.
    suffix = Path(file.filename or "audio").suffix or ".tmp"
    input_path: Path | None = None
    try:
        with NamedTemporaryFile(suffix=suffix, delete=False) as tmp_in:
            input_path = Path(tmp_in.name)
            max_bytes = settings.max_upload_size_mb * 1024 * 1024
            total = 0
            chunk_size = 64 * 1024
            while True:
                chunk = await file.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > max_bytes:
                    raise InvalidRequestError(
                        message=f"File too large ({total / (1024 * 1024):.1f} MB)",
                        hint=f"Maximum upload size is {settings.max_upload_size_mb} MB.",
                    )
                tmp_in.write(chunk)
    except Exception:
        if input_path is not None:
            input_path.unlink(missing_ok=True)
        raise

    # Validate audio duration
    duration = get_audio_duration(input_path)
    if duration is None:
        input_path.unlink(missing_ok=True)
        raise InvalidRequestError(
            message="Unable to determine audio duration",
            hint="The file may be corrupted or not a valid audio format.",
        )
    if duration > settings.max_audio_duration_seconds:
        input_path.unlink(missing_ok=True)
        raise InvalidRequestError(
            message=f"Audio too long ({duration:.0f} seconds)",
            hint=(
                f"Maximum duration is"
                f" {settings.max_audio_duration_seconds // 60} minutes."
            ),
        )

    # Decode to 16kHz mono WAV
    with NamedTemporaryFile(suffix=".wav", delete=False) as tmp_wav:
        wav_path = Path(tmp_wav.name)
    try:
        decode_audio_to_wav(input_path, wav_path)
    except Exception:
        input_path.unlink(missing_ok=True)
        wav_path.unlink(missing_ok=True)
        raise
    finally:
        input_path.unlink(missing_ok=True)

    # Resolve language: explicit value overrides settings;
    # empty string from form means auto-detect (None);
    # omitted means use settings default.
    if language is not None and language != "":
        lang = language
    elif language == "":
        lang = None  # auto-detect
    else:
        lang = settings.whisper_language

    start = time.monotonic()
    request.app.state.queue_depth += 1
    try:
        async with queue_lock:
            try:
                segments, info = await asyncio.wait_for(
                    asyncio.to_thread(
                        engine.transcribe,
                        wav_path,
                        language=lang,
                        beam_size=5,
                        word_timestamps=True,
                        condition_on_previous_text=False,
                    ),
                    timeout=120.0,
                )
            except TimeoutError as exc:
                logger.warning("Transcription timed out")
                raise CompadreError(
                    message="Transcription timed out after 120 seconds",
                    status_code=500,
                    error_type="server_error",
                    hint=(
                        "The audio may be too long or the engine is"
                        " overloaded. Retry later."
                    ),
                ) from exc
    finally:
        request.app.state.queue_depth -= 1
        wav_path.unlink(missing_ok=True)

    duration_sec = time.monotonic() - start
    logger.info(
        "POST /v1/audio/transcriptions format=%s lang=%s segments=%d duration=%.1fs",
        fmt.value,
        info.language,
        len(segments),
        duration_sec,
    )

    # Format output
    plain_text = format_text(segments)
    if fmt == ResponseFormat.text:
        return Response(content=plain_text, media_type="text/plain")
    if fmt == ResponseFormat.json:
        return JSONResponse(content=format_json(plain_text))
    if fmt == ResponseFormat.verbose_json:
        return JSONResponse(
            content=format_verbose_json(
                segments,
                language=info.language or "unknown",
                duration=getattr(info, "duration", 0.0),
            )
        )
    if fmt == ResponseFormat.srt:
        return Response(content=format_srt(segments), media_type="text/plain")
    # fmt == ResponseFormat.vtt
    return Response(content=format_vtt(segments), media_type="text/vtt")
