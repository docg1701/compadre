"""Real-model integration tests: verify every supported audio input format
(ogg/opus, mp3, m4a, wav, webm) decodes and transcribes without error.

Also covers edge cases: corrupted file and empty file rejection.

All tests require ``@pytest.mark.slow`` (real model loaded once per session).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from compadre.exceptions import CompadreError, InvalidRequestError
from compadre.preprocessing import decode_audio_to_wav

pytestmark = pytest.mark.slow


# ── Happy-path: all audio formats ─────────────────────────────────────

_FORMAT_NAMES = {
    "audio.ogg": "ogg/opus",
    "audio.mp3": "mp3",
    "audio.m4a": "m4a",
    "audio.wav": "wav",
    "audio.webm": "webm",
}


@pytest.mark.parametrize(
    "filename",
    ["audio.ogg", "audio.mp3", "audio.m4a", "audio.wav", "audio.webm"],
)
def test_audio_format_decodes_and_transcribes(
    real_engine: Any,
    multi_format_audio_dir: Path,
    tmp_path: Path,
    filename: str,
) -> None:
    """Each supported audio format must decode to WAV and produce transcription."""
    input_path = multi_format_audio_dir / filename
    wav_path = tmp_path / f"{filename}.wav"

    # 1. Decode → must succeed
    result = decode_audio_to_wav(input_path, wav_path)
    assert result == wav_path
    assert wav_path.is_file()
    assert wav_path.stat().st_size > 0, f"decoded {filename} WAV is empty"

    # 2. Transcribe → must return segments with text
    segments, info = real_engine.transcribe(wav_path, language="pt")
    assert isinstance(segments, list)
    assert hasattr(info, "language")
    assert len(segments) > 0, f"{filename} produced zero segments"
    text = " ".join(s.text.strip() for s in segments)
    assert len(text) > 0, f"{filename} transcription text is empty"


# ── Edge case: corrupted file → InvalidRequestError from decode ───────


def test_corrupted_file_raises_invalid_request_error(tmp_path: Path) -> None:
    """A file of random bytes renamed .ogg must fail decode with
    InvalidRequestError, not a raw subprocess traceback."""
    corrupted = tmp_path / "corrupted.ogg"
    corrupted.write_bytes(b"\xff\xfe\xfd\xfc" * 1000)

    with pytest.raises(InvalidRequestError):
        decode_audio_to_wav(corrupted, tmp_path / "out.wav")


# ── Edge case: empty file → InvalidRequestError from engine ───────────


def test_empty_wav_raises_invalid_request_error(
    real_engine: Any, tmp_path: Path
) -> None:
    """An empty WAV file must be rejected by the engine with
    InvalidRequestError (no segfault, no raw unhandled traceback)."""
    empty_wav = tmp_path / "empty.wav"
    # Create a truly empty file (0 bytes)
    empty_wav.write_bytes(b"")

    with pytest.raises((InvalidRequestError, CompadreError)):
        real_engine.transcribe(empty_wav, language="pt")
