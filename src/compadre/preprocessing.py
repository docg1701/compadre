"""Audio preprocessing — decode any format to 16 kHz mono PCM WAV."""

from __future__ import annotations

import subprocess
from pathlib import Path

from compadre.exceptions import InvalidRequestError
from compadre.logging import get_logger

logger = get_logger("request")


def decode_audio_to_wav(
    input_path: Path,
    output_path: Path,
    *,
    sample_rate: int = 16000,
    channels: int = 1,
) -> Path:
    """Decode any audio format to 16 kHz mono PCM s16le WAV via ffmpeg.

    Args:
        input_path: Path to the uploaded audio file (OPUS, MP3, M4A, WAV, etc.)
        output_path: Path for the output WAV file.
        sample_rate: Target sample rate in Hz (default 16000 for Whisper).
        channels: Number of channels (default 1 for mono).

    Returns:
        Path to the decoded WAV file.

    Raises:
        InvalidRequestError: If ffmpeg fails or produces no output.
    """
    cmd = [
        "ffmpeg",
        "-y",
        "-i",
        str(input_path),
        "-vn",
        "-acodec",
        "pcm_s16le",
        "-ar",
        str(sample_rate),
        "-ac",
        str(channels),
        str(output_path),
    ]

    try:
        _result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired as exc:
        logger.error("Audio decode timed out after 60s")
        raise InvalidRequestError(
            message="Audio decoding timed out",
            hint="The file may be too large or corrupted. Try a shorter clip.",
        ) from exc
    except subprocess.CalledProcessError as exc:
        logger.error("Audio decode failed: %s", exc.stderr)
        raise InvalidRequestError(
            message="Audio decoding failed — unsupported or corrupted file",
            hint="Ensure the file is a valid audio format (OPUS, MP3, M4A, WAV, etc.).",
        ) from exc

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise InvalidRequestError(
            message="Audio decoding produced empty output",
            hint="The uploaded file may not contain valid audio data.",
        )

    return output_path


def get_audio_duration(audio_path: Path) -> float | None:
    """Return audio duration in seconds via ffprobe.

    Returns ``None`` if detection fails (corrupted file or unsupported format).
    """
    cmd = [
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        str(audio_path),
    ]
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return float(result.stdout.strip())
    except (subprocess.TimeoutExpired, ValueError, OSError) as exc:
        logger.warning("ffprobe duration detection failed: %s", exc)
    return None
