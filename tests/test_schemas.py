"""Tests for request/response schemas.

Lean by design (test-suite review 2026-04-25): pydantic Field-constraint
tests collapsed (testing-the-framework); kept ResponseFormat enum
verification.
"""

import pytest

from compadre.schemas import ResponseFormat


def test_response_format_values() -> None:
    assert ResponseFormat.text.value == "text"
    assert ResponseFormat.json.value == "json"
    assert ResponseFormat.verbose_json.value == "verbose_json"
    assert ResponseFormat.srt.value == "srt"
    assert ResponseFormat.vtt.value == "vtt"


def test_response_format_from_string() -> None:
    assert ResponseFormat("text") == ResponseFormat.text
    assert ResponseFormat("json") == ResponseFormat.json
    assert ResponseFormat("verbose_json") == ResponseFormat.verbose_json
    assert ResponseFormat("srt") == ResponseFormat.srt
    assert ResponseFormat("vtt") == ResponseFormat.vtt


def test_response_format_rejects_invalid() -> None:
    with pytest.raises(ValueError):
        ResponseFormat("xml")
