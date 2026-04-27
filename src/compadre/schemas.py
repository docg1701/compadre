"""Request and response schemas for Compadre STT API."""

from enum import StrEnum


class ResponseFormat(StrEnum):
    """Supported transcription output formats."""

    text = "text"
    json = "json"
    verbose_json = "verbose_json"
    srt = "srt"
    vtt = "vtt"
