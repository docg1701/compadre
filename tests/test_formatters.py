"""Unit tests for transcription output formatters.

Covers all five ``ResponseFormat`` variants: text, json, verbose_json,
srt, and vtt. Every test builds mock segments via ``_make_segment`` so
no real model inference is required.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def _make_segment(
    text: str,
    start: float,
    end: float,
    *,
    words: list[tuple[str, float, float, float]] | None = None,
) -> MagicMock:
    """Build a mock ``faster_whisper.transcribe.Segment``.

    Args:
        text: Segment text (may include whitespace; ``format_*`` strip it).
        start: Segment start time in seconds.
        end: Segment end time in seconds.
        words: Optional list of ``(word, start, end, probability)`` tuples.
            When ``None`` the ``words`` attribute is ``None`` (no word data).

    Returns:
        A ``MagicMock`` whose attribute access mimics the real Segment.
    """
    seg = MagicMock()
    seg.text = text
    seg.start = start
    seg.end = end
    if words is not None:
        seg.words = [
            _make_word(w, ws, we, wp) for w, ws, we, wp in words
        ]
    else:
        seg.words = None
    return seg


def _make_word(
    word: str, start: float, end: float, probability: float
) -> MagicMock:
    """Build a mock ``faster_whisper.transcribe.Word``."""
    w = MagicMock()
    w.word = word
    w.start = start
    w.end = end
    w.probability = probability
    return w


# ---------------------------------------------------------------------------
# format_text
# ---------------------------------------------------------------------------


class TestFormatText:
    """Tests for ``format_text()``."""

    def test_joins_stripped_segment_texts(self) -> None:
        from compadre.formatters import format_text

        segments = [
            _make_segment(" Hello ", 0.0, 1.0),
            _make_segment(" world ", 1.0, 2.0),
            _make_segment("!", 2.0, 3.0),
        ]
        assert format_text(segments) == "Hello world !"

    def test_empty_list_returns_empty_string(self) -> None:
        from compadre.formatters import format_text

        assert format_text([]) == ""


# ---------------------------------------------------------------------------
# format_json
# ---------------------------------------------------------------------------


class TestFormatJson:
    """Tests for ``format_json()``."""

    def test_returns_dict_with_text_key(self) -> None:
        from compadre.formatters import format_json

        assert format_json("Hello world") == {"text": "Hello world"}

    def test_empty_text_produces_empty_dict_value(self) -> None:
        from compadre.formatters import format_json

        assert format_json("") == {"text": ""}


# ---------------------------------------------------------------------------
# format_verbose_json
# ---------------------------------------------------------------------------


class TestFormatVerboseJson:
    """Tests for ``format_verbose_json()``."""

    def test_includes_segments_language_and_duration(self) -> None:
        from compadre.formatters import format_verbose_json

        segments = [
            _make_segment(
                "Hello",
                0.0,
                0.5,
                words=[("Hello", 0.0, 0.5, 0.99)],
            ),
            _make_segment(
                "world",
                0.6,
                1.0,
                words=[("world", 0.6, 1.0, 0.98)],
            ),
        ]
        result = format_verbose_json(segments, language="pt", duration=1.5)

        assert result["text"] == "Hello world"
        assert result["language"] == "pt"
        assert result["duration"] == 1.5
        assert len(result["segments"]) == 2

        seg0 = result["segments"][0]  # type: ignore[index]
        assert seg0["text"] == "Hello"
        assert seg0["start"] == 0.0
        assert seg0["end"] == 0.5
        assert len(seg0["words"]) == 1
        assert seg0["words"][0]["word"] == "Hello"
        assert seg0["words"][0]["start"] == 0.0
        assert seg0["words"][0]["end"] == 0.5
        assert seg0["words"][0]["probability"] == 0.99

    def test_words_is_empty_list_when_none(self) -> None:
        """When a segment has ``words=None`` (no word-level data), the
        verbose_json output uses an empty list."""
        from compadre.formatters import format_verbose_json

        segments = [_make_segment("Hi", 0.0, 1.0, words=None)]
        result = format_verbose_json(segments, language="en", duration=1.0)

        assert result["segments"][0]["words"] == []  # type: ignore[index]

    def test_empty_segments_list(self) -> None:
        from compadre.formatters import format_verbose_json

        result = format_verbose_json([], language="pt", duration=0.0)

        assert result["text"] == ""
        assert result["segments"] == []


# ---------------------------------------------------------------------------
# format_srt
# ---------------------------------------------------------------------------


class TestFormatSrt:
    """Tests for ``format_srt()``."""

    def test_returns_valid_srt_with_correct_timestamps(self) -> None:
        from compadre.formatters import format_srt

        segments = [
            _make_segment("Hello", 0.0, 2.5),
            _make_segment("world", 3.0, 5.75),
        ]
        result = format_srt(segments)

        expected = (
            "1\n"
            "00:00:00,000 --> 00:00:02,500\n"
            "Hello\n"
            "\n"
            "2\n"
            "00:00:03,000 --> 00:00:05,750\n"
            "world\n"
        )
        assert result == expected

    def test_strips_segment_text_whitespace(self) -> None:
        from compadre.formatters import format_srt

        segments = [_make_segment("  padded  ", 0.0, 1.0)]
        result = format_srt(segments)

        assert "padded" in result
        assert "  padded" not in result

    def test_empty_segments_returns_empty_string(self) -> None:
        from compadre.formatters import format_srt

        assert format_srt([]) == ""


# ---------------------------------------------------------------------------
# format_vtt
# ---------------------------------------------------------------------------


class TestFormatVtt:
    """Tests for ``format_vtt()``."""

    def test_returns_valid_vtt_with_period_separators(self) -> None:
        from compadre.formatters import format_vtt

        segments = [
            _make_segment("Hello", 0.0, 2.5),
            _make_segment("world", 3.0, 5.75),
        ]
        result = format_vtt(segments)

        expected = (
            "WEBVTT\n"
            "\n"
            "00:00:00.000 --> 00:00:02.500\n"
            "Hello\n"
            "\n"
            "00:00:03.000 --> 00:00:05.750\n"
            "world\n"
        )
        assert result == expected

    def test_empty_segments_returns_header_only(self) -> None:
        from compadre.formatters import format_vtt

        assert format_vtt([]) == "WEBVTT\n"
