"""Real-model integration tests: verify every ``response_format`` produces
structurally valid output from actual faster-whisper inference.

All tests require ``@pytest.mark.slow`` (real model loaded once per session).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest

from compadre.formatters import (
    format_json,
    format_srt,
    format_text,
    format_verbose_json,
    format_vtt,
)
from compadre.preprocessing import decode_audio_to_wav

pytestmark = pytest.mark.slow


def _decode_reference(reference_audio_path: Path, tmp_path: Path) -> Path:
    """Decode the reference OGG to 16kHz mono WAV for engine consumption."""
    wav = tmp_path / "decoded.wav"
    decode_audio_to_wav(reference_audio_path, wav)
    return wav


# ── text ──────────────────────────────────────────────────────────────


def test_format_text_output_structure(
    real_engine: Any, reference_audio_path: Path, tmp_path: Path
) -> None:
    """``text`` format: non-empty string, no JSON wrapper, plain text."""
    wav = _decode_reference(reference_audio_path, tmp_path)
    segments, _info = real_engine.transcribe(wav, language="pt")
    output = format_text(segments)
    assert isinstance(output, str)
    assert len(output.strip()) > 0, "text output must not be empty"
    assert not output.strip().startswith("{"), "text must not be JSON"


# ── json ──────────────────────────────────────────────────────────────


def test_format_json_output_structure(
    real_engine: Any, reference_audio_path: Path, tmp_path: Path
) -> None:
    """``json`` format: ``{"text": "..."}`` with string value."""
    wav = _decode_reference(reference_audio_path, tmp_path)
    segments, _info = real_engine.transcribe(wav, language="pt")
    output = format_json(format_text(segments))
    assert isinstance(output, dict)
    assert "text" in output
    assert isinstance(output["text"], str)
    assert len(output["text"].strip()) > 0


# ── verbose_json ──────────────────────────────────────────────────────


def test_format_verbose_json_output_structure(
    real_engine: Any, reference_audio_path: Path, tmp_path: Path
) -> None:
    """``verbose_json`` format: ``text``, ``language``, ``duration``, ``segments``."""
    wav = _decode_reference(reference_audio_path, tmp_path)
    segments, info = real_engine.transcribe(wav, language="pt")
    output = format_verbose_json(
        segments,
        language=info.language or "unknown",
        duration=getattr(info, "duration", 0.0),
    )
    assert isinstance(output, dict)
    for key in ("text", "language", "duration", "segments"):
        assert key in output, f"verbose_json missing key: {key}"
    assert isinstance(output["text"], str)
    assert isinstance(output["language"], str)
    assert isinstance(output["duration"], (int, float))
    assert isinstance(output["segments"], list)

    # Each segment carries start, end, text, words
    for seg in output["segments"]:
        assert isinstance(seg, dict)
        for seg_key in ("start", "end", "text", "words"):
            assert seg_key in seg, f"segment missing key: {seg_key}"
        assert isinstance(seg["start"], (int, float))
        assert isinstance(seg["end"], (int, float))
        assert isinstance(seg["text"], str)
        assert isinstance(seg["words"], list)
        for w in seg["words"]:
            assert isinstance(w, dict)
            assert "word" in w
            assert "start" in w
            assert "end" in w
            assert "probability" in w


# ── srt ───────────────────────────────────────────────────────────────

_SRT_LINE_RE = re.compile(r"^\d+$")
_SRT_TIMESTAMP_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}$"
)


def test_format_srt_output_structure(
    real_engine: Any, reference_audio_path: Path, tmp_path: Path
) -> None:
    """``srt`` format: indexed blocks with valid timestamps, non-empty text."""
    wav = _decode_reference(reference_audio_path, tmp_path)
    segments, _info = real_engine.transcribe(wav, language="pt")
    output = format_srt(segments)
    assert isinstance(output, str)
    assert len(output.strip()) > 0

    # Parse SRT blocks: numbered block, timestamp line, text line(s), blank line
    blocks = [b for b in output.strip().split("\n\n") if b.strip()]
    assert len(blocks) > 0, "SRT must contain at least one subtitle block"

    for i, block in enumerate(blocks):
        lines = block.split("\n")
        # Line 0: index number
        assert _SRT_LINE_RE.match(lines[0]), (
            f"SRT block {i} line 0 should be an index number, got: {lines[0]!r}"
        )
        # Line 1: timestamp
        assert _SRT_TIMESTAMP_RE.match(lines[1]), (
            f"SRT block {i} line 1 should be a timestamp, got: {lines[1]!r}"
        )
        # Line 2+: non-empty text
        text_lines = lines[2:]
        assert len(text_lines) > 0, f"SRT block {i} missing text"
        assert any(line.strip() for line in text_lines), (
            f"SRT block {i} text must not be all empty"
        )


# ── vtt ───────────────────────────────────────────────────────────────

_VTT_TIMESTAMP_RE = re.compile(
    r"^\d{2}:\d{2}:\d{2}\.\d{3} --> \d{2}:\d{2}:\d{2}\.\d{3}$"
)


def test_format_vtt_output_structure(
    real_engine: Any, reference_audio_path: Path, tmp_path: Path
) -> None:
    """``vtt`` format: WEBVTT header, period-separated timestamps, non-empty cues."""
    wav = _decode_reference(reference_audio_path, tmp_path)
    segments, _info = real_engine.transcribe(wav, language="pt")
    output = format_vtt(segments)
    assert isinstance(output, str)

    lines = output.strip().split("\n")
    assert lines[0] == "WEBVTT", f"VTT must start with WEBVTT, got: {lines[0]!r}"

    # After header, cues: timestamp, text, blank
    # We only check that at least one valid timestamp appears
    timestamps_found = 0
    for line in lines[1:]:
        if _VTT_TIMESTAMP_RE.match(line):
            timestamps_found += 1
    assert timestamps_found > 0, "VTT must contain at least one valid cue timestamp"
    # VTT uses . not , in timestamps
    assert "," not in lines[1], "VTT timestamps must use period (.) not comma (,)"


# ── Edge case: empty segments ─────────────────────────────────────────


def test_all_formatters_handle_empty_segments() -> None:
    """Each formatter must gracefully handle an empty segments list."""
    from faster_whisper.transcribe import Segment

    empty: list[Segment] = []
    assert format_text(empty) == ""
    assert format_json("") == {"text": ""}
    verbose = format_verbose_json(empty, language="pt", duration=0.0)
    assert verbose["text"] == ""
    assert verbose["segments"] == []
    assert format_srt(empty) == ""
    assert format_vtt(empty) == "WEBVTT\n"
