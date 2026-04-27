"""Output formatting for transcription response formats."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from faster_whisper.transcribe import Segment


def format_text(segments: list[Segment]) -> str:
    """Plain text: concatenate all segment texts."""
    return " ".join(s.text.strip() for s in segments)


def format_json(text: str) -> dict[str, str]:
    """JSON mode: {"text": "..."}"""
    return {"text": text}


def format_verbose_json(
    segments: list[Segment],
    language: str,
    duration: float,
) -> dict[str, object]:
    """OpenAI verbose_json format with segments and words."""
    return {
        "text": format_text(segments),
        "language": language,
        "duration": duration,
        "segments": [
            {
                "start": s.start,
                "end": s.end,
                "text": s.text.strip(),
                "words": [
                    {
                        "word": w.word,
                        "start": w.start,
                        "end": w.end,
                        "probability": w.probability,
                    }
                    for w in (s.words or [])
                ],
            }
            for s in segments
        ],
    }


def _format_srt_timestamp(seconds: float) -> str:
    """Format seconds to SRT timestamp: HH:MM:SS,mmm.

    Copied verbatim from youtube-guru/youtube_pipeline/srt_utils.py.
    """
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"


def format_srt(segments: list[Segment]) -> str:
    """SRT subtitle format."""
    lines: list[str] = []
    for i, segment in enumerate(segments, start=1):
        lines.append(str(i))
        lines.append(
            f"{_format_srt_timestamp(segment.start)}"
            f" --> {_format_srt_timestamp(segment.end)}"
        )
        lines.append(segment.text.strip())
        lines.append("")
    return "\n".join(lines)


def format_vtt(segments: list[Segment]) -> str:
    """WebVTT subtitle format."""
    lines = ["WEBVTT\n"]
    for segment in segments:
        start = _format_srt_timestamp(segment.start).replace(",", ".")
        end = _format_srt_timestamp(segment.end).replace(",", ".")
        lines.append(f"{start} --> {end}")
        lines.append(segment.text.strip())
        lines.append("")
    return "\n".join(lines)
