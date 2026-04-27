"""Unit tests for audio preprocessing utilities.

Covers ``decode_audio_to_wav`` (ffmpeg wrapper) and ``get_audio_duration``
(ffprobe wrapper). All subprocess calls are fully mocked — no real ffmpeg
or audio files are required.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from compadre.exceptions import InvalidRequestError
from compadre.preprocessing import decode_audio_to_wav, get_audio_duration

# ---------------------------------------------------------------------------
# decode_audio_to_wav
# ---------------------------------------------------------------------------


class TestDecodeAudioToWav:
    """Tests for ``decode_audio_to_wav()``."""

    def test_returns_output_path_on_success(self, tmp_path: Path) -> None:
        """When ffmpeg exits cleanly and the output file is non-empty,
        the output path is returned."""
        input_path = tmp_path / "input.opus"
        input_path.write_bytes(b"fake opus data")
        output_path = tmp_path / "output.wav"
        # Pre-create a non-empty file so the post-decode existence check passes.
        output_path.write_bytes(b"fake wav data")

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            result = decode_audio_to_wav(input_path, output_path)

        assert result == output_path
        mock_run.assert_called_once()

    def test_raises_invalid_request_on_nonzero_returncode(
        self, tmp_path: Path
    ) -> None:
        """ffmpeg returning non-zero exit code raises
        ``InvalidRequestError`` with a corruption hint."""
        input_path = tmp_path / "corrupt.opus"
        input_path.write_bytes(b"not valid audio")
        output_path = tmp_path / "output.wav"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.CalledProcessError(
                1, "ffmpeg", stderr="Invalid data found"
            )

            with pytest.raises(
                InvalidRequestError, match="unsupported or corrupted"
            ):
                decode_audio_to_wav(input_path, output_path)

    def test_raises_invalid_request_when_output_file_missing(
        self, tmp_path: Path
    ) -> None:
        """If ffmpeg exits zero but produces no output file, raise
        ``InvalidRequestError``."""
        input_path = tmp_path / "input.opus"
        input_path.write_bytes(b"fake data")
        output_path = tmp_path / "output.wav"
        # Do NOT create output_path — the function will see it missing.

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(InvalidRequestError, match="empty output"):
                decode_audio_to_wav(input_path, output_path)

    def test_raises_invalid_request_when_output_file_is_empty(
        self, tmp_path: Path
    ) -> None:
        """If ffmpeg exits zero but the output file is zero bytes, raise
        ``InvalidRequestError``."""
        input_path = tmp_path / "input.opus"
        input_path.write_bytes(b"fake data")
        output_path = tmp_path / "output.wav"
        output_path.write_bytes(b"")  # zero bytes

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)

            with pytest.raises(InvalidRequestError, match="empty output"):
                decode_audio_to_wav(input_path, output_path)

    def test_raises_invalid_request_on_timeout(self, tmp_path: Path) -> None:
        """ffmpeg timeout raises ``InvalidRequestError``."""
        input_path = tmp_path / "huge.opus"
        input_path.write_bytes(b"very large file")
        output_path = tmp_path / "output.wav"

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("ffmpeg", 60)

            with pytest.raises(InvalidRequestError, match="timed out"):
                decode_audio_to_wav(input_path, output_path)


# ---------------------------------------------------------------------------
# get_audio_duration
# ---------------------------------------------------------------------------


class TestGetAudioDuration:
    """Tests for ``get_audio_duration()``."""

    def test_returns_float_duration_on_success(self, tmp_path: Path) -> None:
        """ffprobe prints a valid float → parses and returns it."""
        audio_path = tmp_path / "test.wav"
        audio_path.write_bytes(b"dummy wav")

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "12.345\n"
            mock_run.return_value = mock_result

            duration = get_audio_duration(audio_path)

        assert duration == 12.345
        mock_run.assert_called_once()

    def test_returns_none_on_nonzero_returncode(self, tmp_path: Path) -> None:
        """ffprobe exit code != 0 → returns None."""
        audio_path = tmp_path / "test.wav"
        audio_path.write_bytes(b"corrupt")

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 1
            mock_result.stdout = ""
            mock_run.return_value = mock_result

            duration = get_audio_duration(audio_path)

        assert duration is None

    def test_returns_none_on_empty_stdout(self, tmp_path: Path) -> None:
        """ffprobe exits zero but stdout is blank → returns None."""
        audio_path = tmp_path / "test.wav"
        audio_path.write_bytes(b"dummy")

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "\n"  # only whitespace
            mock_run.return_value = mock_result

            duration = get_audio_duration(audio_path)

        assert duration is None

    def test_returns_none_on_non_numeric_stdout(self, tmp_path: Path) -> None:
        """ffprobe stdout is not a float → ``ValueError`` caught, returns
        None."""
        audio_path = tmp_path / "test.wav"
        audio_path.write_bytes(b"dummy")

        with patch("subprocess.run") as mock_run:
            mock_result = MagicMock()
            mock_result.returncode = 0
            mock_result.stdout = "N/A\n"
            mock_run.return_value = mock_result

            duration = get_audio_duration(audio_path)

        assert duration is None

    def test_returns_none_on_timeout_expired(self, tmp_path: Path) -> None:
        """ffprobe timeout → caught, returns None."""
        audio_path = tmp_path / "test.wav"
        audio_path.write_bytes(b"dummy")

        with patch("subprocess.run") as mock_run:
            mock_run.side_effect = subprocess.TimeoutExpired("ffprobe", 30)

            duration = get_audio_duration(audio_path)

        assert duration is None
