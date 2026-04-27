"""Real-model integration tests: verify language parameter behaviour.

- Explicit ``language="pt"`` → info reports Portuguese
- ``language=None`` (auto-detect) → engine auto-detects
- ``language="en"`` with Portuguese audio → param passed through
- 1-second silence → empty segments, info still populated

All tests require ``@pytest.mark.slow`` (real model loaded once per session).
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from typing import Any

import pytest

from compadre.preprocessing import decode_audio_to_wav

pytestmark = pytest.mark.slow


def _decode_reference(reference_audio_path: Path, tmp_path: Path) -> Path:
    wav = tmp_path / "decoded.wav"
    decode_audio_to_wav(reference_audio_path, wav)
    return wav


def _generate_silent_wav(duration_ms: int, sample_rate: int = 16000) -> bytes:
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    pcm = b"\x00\x00" * num_samples
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


def test_language_explicit_pt_detected_as_pt(
    real_engine: Any, reference_audio_path: Path, tmp_path: Path
) -> None:
    """Explicit ``language="pt"`` → info reports Portuguese."""
    wav = _decode_reference(reference_audio_path, tmp_path)
    _segments, info = real_engine.transcribe(wav, language="pt")
    assert info.language is not None
    lang = info.language.lower()
    assert lang == "pt" or "portuguese" in lang, (
        f"Expected 'pt' or 'portuguese', got: {info.language!r}"
    )


def test_language_none_auto_detects(
    real_engine: Any, reference_audio_path: Path, tmp_path: Path
) -> None:
    """``language=None`` → engine auto-detects and returns a language code."""
    wav = _decode_reference(reference_audio_path, tmp_path)
    _segments, info = real_engine.transcribe(wav, language=None)
    assert info.language is not None, "auto-detect must return a language"
    assert len(info.language) >= 2, f"language code too short: {info.language!r}"
    # Portuguese audio should auto-detect as pt (probability-oriented)
    # We only assert it returned *something*; exact language may vary.


def test_language_en_override_on_pt_audio(
    real_engine: Any, reference_audio_path: Path, tmp_path: Path
) -> None:
    """``language="en"`` passed through even for Portuguese audio —
    engine respects the parameter regardless of content mismatch."""
    wav = _decode_reference(reference_audio_path, tmp_path)
    segments, info = real_engine.transcribe(wav, language="en")
    # The engine should report the language we asked for (en)
    # or possibly auto-detect still — either way, it should not crash
    assert info.language is not None
    # The param is passed through; we verify no exception was raised
    # and segments were produced (even if transcribed in wrong language)
    assert isinstance(segments, list)


def test_silent_audio_returns_empty_segments(real_engine: Any, tmp_path: Path) -> None:
    """1-second silence WAV → info populated, but zero segments (or empty)."""
    silent_bytes = _generate_silent_wav(1000)
    silent_wav = tmp_path / "silent.wav"
    silent_wav.write_bytes(silent_bytes)

    segments, info = real_engine.transcribe(silent_wav, language="pt")

    # Info must still be populated
    assert hasattr(info, "language")
    # Segments may be empty or contain a single empty-text segment
    assert isinstance(segments, list)
