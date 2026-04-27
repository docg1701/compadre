"""Unit tests for WhisperEngine (STT transcription engine).

Tests the faster-whisper wrapper: model loading, warm-up, and
transcription delegation. All external calls are fully mocked — no
real model is loaded.
"""

from __future__ import annotations

import io
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from compadre.exceptions import CompadreError, InvalidRequestError
from compadre.stt import WhisperEngine


def _generate_minimal_wav(duration_ms: int = 500) -> bytes:
    """Return a minimal 16-bit mono PCM WAV with silent samples.

    Used to satisfy the file-exists and non-empty-file checks inside
    ``WhisperEngine.transcribe()`` without writing to disk in every test.
    """
    sample_rate = 16000
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    pcm_data = b"\x00\x00" * num_samples
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()


class TestLoadModel:
    """Tests for ``WhisperEngine.load_model()``."""

    def test_sets_model_attribute_on_success(self) -> None:
        """Mock ``faster_whisper.WhisperModel`` constructor and verify the
        engine stores the created instance and flips ``is_loaded``."""
        engine = WhisperEngine()
        mock_instance = MagicMock()

        with patch("faster_whisper.WhisperModel", return_value=mock_instance):
            engine.load_model()

        assert engine._model is mock_instance
        assert engine.is_loaded is True

    def test_propagates_constructor_failure(self) -> None:
        """If ``WhisperModel(...)`` raises, the exception is not caught —
        it propagates straight to the caller. Verify the engine stays
        unloaded."""
        engine = WhisperEngine()

        with patch(
            "faster_whisper.WhisperModel",
            side_effect=RuntimeError("CUDA driver not found"),
        ), pytest.raises(RuntimeError, match="CUDA driver not found"):
            engine.load_model()

        assert engine._model is None
        assert engine.is_loaded is False


class TestWarmUp:
    """Tests for ``WhisperEngine.warm_up()``."""

    def test_delegates_to_model_transcribe(self) -> None:
        """``warm_up`` generates a silent WAV, writes it to a temp file,
        and calls ``_model.transcribe()``. Verify the call happens and
        the argument is a WAV path."""
        engine = WhisperEngine()
        mock_model = MagicMock()
        # transcribe returns (segments_iterable, info)
        mock_model.transcribe.return_value = ([], MagicMock())
        engine._model = mock_model

        engine.warm_up()

        mock_model.transcribe.assert_called_once()
        call_args = mock_model.transcribe.call_args
        # First positional arg must be a string ending in .wav
        audio_arg: str = call_args[0][0]
        assert audio_arg.endswith(".wav")

    def test_raises_compadre_error_when_model_not_loaded(self) -> None:
        """Calling ``warm_up`` before ``load_model`` raises
        ``CompadreError`` with a clear hint."""
        engine = WhisperEngine()

        with pytest.raises(CompadreError, match="Model not loaded"):
            engine.warm_up()

    def test_survives_transcription_failure(self) -> None:
        """If the internal ``transcribe`` call raises, ``warm_up`` logs a
        warning and returns normally — it does not propagate the error."""
        engine = WhisperEngine()
        mock_model = MagicMock()
        mock_model.transcribe.side_effect = RuntimeError("segfault in kernel")
        engine._model = mock_model

        # Must not raise
        engine.warm_up()

        mock_model.transcribe.assert_called_once()


class TestTranscribe:
    """Tests for ``WhisperEngine.transcribe()``."""

    def test_delegates_to_model_and_returns_segments_and_info(
        self, tmp_path: Path
    ) -> None:
        """Set a mock model, call ``transcribe`` with a valid WAV file,
        and verify the return tuple contains the exact objects the mock
        returned."""
        engine = WhisperEngine()
        mock_model = MagicMock()
        mock_segments = [MagicMock(), MagicMock()]
        mock_info = MagicMock()
        mock_info.language = "pt"
        mock_info.language_probability = 0.99
        # faster-whisper returns an iterator for segments; wrap in iter()
        mock_model.transcribe.return_value = (iter(mock_segments), mock_info)
        engine._model = mock_model

        audio_path = tmp_path / "valid.wav"
        audio_path.write_bytes(_generate_minimal_wav())

        segments, info = engine.transcribe(audio_path)

        mock_model.transcribe.assert_called_once()
        assert segments == mock_segments
        assert info is mock_info

    def test_raises_compadre_error_when_model_not_loaded(
        self, tmp_path: Path
    ) -> None:
        """``transcribe`` before ``load_model`` raises ``CompadreError``."""
        engine = WhisperEngine()
        audio_path = tmp_path / "valid.wav"
        audio_path.write_bytes(_generate_minimal_wav())

        with pytest.raises(CompadreError, match="Model not loaded"):
            engine.transcribe(audio_path)

    def test_raises_invalid_request_for_missing_file(self, tmp_path: Path) -> None:
        """Non-existent audio path raises ``InvalidRequestError``."""
        engine = WhisperEngine()
        engine._model = MagicMock()

        audio_path = tmp_path / "does_not_exist.wav"

        with pytest.raises(InvalidRequestError, match="not found"):
            engine.transcribe(audio_path)

    def test_raises_invalid_request_for_empty_file(self, tmp_path: Path) -> None:
        """Zero-byte audio file raises ``InvalidRequestError``."""
        engine = WhisperEngine()
        engine._model = MagicMock()
        audio_path = tmp_path / "empty.wav"
        audio_path.write_bytes(b"")

        with pytest.raises(InvalidRequestError, match="empty"):
            engine.transcribe(audio_path)

    def test_raises_invalid_request_error_on_unsupported_language(
        self, tmp_path: Path
    ) -> None:
        """A ``ValueError`` from faster-whisper (e.g. unsupported language
        code) is converted to ``InvalidRequestError`` (400) with a
        sanitized message — no internal list of valid codes is leaked
        (SEC-001)."""
        engine = WhisperEngine()
        mock_model = MagicMock()
        mock_model.transcribe.side_effect = ValueError(
            "'NOTAVALIDLANG' is not a valid language code "
            "(accepted language codes: af, am, ar, ...)"
        )
        engine._model = mock_model

        audio_path = tmp_path / "valid.wav"
        audio_path.write_bytes(_generate_minimal_wav())

        with pytest.raises(InvalidRequestError, match="Unsupported language code"):
            engine.transcribe(audio_path, language="NOTAVALIDLANG")

    def test_raises_compadre_error_on_transcription_failure(
        self, tmp_path: Path
    ) -> None:
        """If the underlying ``faster_whisper`` call raises an unexpected
        exception, it is wrapped in ``CompadreError`` (500) with a generic
        message — no internal detail is leaked to the client."""
        engine = WhisperEngine()
        mock_model = MagicMock()
        mock_model.transcribe.side_effect = RuntimeError(
            "CTranslate2 kernel failed"
        )
        engine._model = mock_model

        audio_path = tmp_path / "valid.wav"
        audio_path.write_bytes(_generate_minimal_wav())

        with pytest.raises(CompadreError, match="internal server error"):
            engine.transcribe(audio_path)
