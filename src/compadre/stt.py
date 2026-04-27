"""STT engine service — faster-whisper CTranslate2 integration."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, Any

from compadre.exceptions import CompadreError, InvalidRequestError
from compadre.logging import get_logger

if TYPE_CHECKING:
    from faster_whisper.transcribe import Segment, TranscriptionInfo

logger = get_logger("request")
startup_logger = get_logger("startup")


class WhisperEngine:
    """Wraps faster-whisper for STT transcription."""

    def __init__(self) -> None:
        self._model: Any | None = None

    @property
    def is_loaded(self) -> bool:
        """Return True if the Whisper model is loaded."""
        return self._model is not None

    def load_model(
        self,
        model_size: str = "large-v3-turbo",
        device: str = "cpu",
        compute_type: str = "int8",
        cpu_threads: int = 4,
    ) -> None:
        """Load the faster-whisper model into memory.

        On first call, faster-whisper auto-downloads from HuggingFace Hub
        if the model is not cached (HF_HOME controls cache location).
        Subsequent calls load from cache.
        """
        from faster_whisper import WhisperModel

        startup_logger.info(
            "Loading Whisper model"
            " (size=%s, device=%s, compute_type=%s, threads=%d)...",
            model_size,
            device,
            compute_type,
            cpu_threads,
        )
        self._model = WhisperModel(
            model_size,
            device=device,
            compute_type=compute_type,
            cpu_threads=cpu_threads,
        )
        startup_logger.info("Whisper model loaded")

    def warm_up(self) -> None:
        """Run a minimal transcription to preload model internals."""
        if self._model is None:
            raise CompadreError(
                message="Model not loaded — call load_model() first",
                status_code=500,
                error_type="server_error",
                hint="Whisper engine failed to load during startup.",
            )
        startup_logger.info("Running warm-up transcription...")
        # Create a tiny silent WAV (1 second, 16kHz mono)
        _warmup_audio = _generate_silent_wav(duration_ms=1000, sample_rate=16000)
        with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tmp:
            tmp.write(_warmup_audio)
            tmp_path = Path(tmp.name)
        try:
            segments, _info = self._model.transcribe(
                str(tmp_path),
                beam_size=1,
                word_timestamps=False,
                language="pt",
                condition_on_previous_text=False,
            )
            list(segments)  # consume generator
            startup_logger.info("Warm-up complete")
        except Exception as exc:
            startup_logger.warning("Warm-up transcription failed: %s", exc)
        finally:
            tmp_path.unlink(missing_ok=True)

    def transcribe(
        self,
        audio_path: Path,
        *,
        language: str | None = "pt",
        beam_size: int = 5,
        word_timestamps: bool = True,
        condition_on_previous_text: bool = False,
    ) -> tuple[list[Segment], TranscriptionInfo]:
        """Run transcription and return segments + info.

        Args:
            audio_path: Path to the decoded 16kHz mono WAV file.
            language: ISO language code (default "pt"; None for auto-detect).
            beam_size: Beam search width (default 5, battle-tested).
            word_timestamps: Emit per-word timestamps (default True, cheap).
            condition_on_previous_text: Disable to prevent hallucination
                cascades on long audio (default False, non-negotiable).

        Returns:
            Tuple of (segments list, transcription info object).

        Raises:
            InvalidRequestError: If audio file is missing or empty.
            CompadreError: If transcription fails.
        """
        if self._model is None:
            raise CompadreError(
                message="Model not loaded",
                status_code=500,
                error_type="server_error",
                hint="Whisper engine failed to load during startup.",
            )

        if not audio_path.is_file():
            raise InvalidRequestError(
                message=f"Audio file not found: {audio_path}",
                hint="Verify the uploaded file was saved correctly.",
            )

        if audio_path.stat().st_size == 0:
            raise InvalidRequestError(
                message="Audio file is empty",
                hint="Upload a non-empty audio file.",
            )

        try:
            segments_iter, info = self._model.transcribe(
                str(audio_path),
                beam_size=beam_size,
                word_timestamps=word_timestamps,
                language=language,
                condition_on_previous_text=condition_on_previous_text,
            )
            segments = list(segments_iter)
            logger.info(
                "Transcription complete: %d segments, language=%s, prob=%.2f",
                len(segments),
                info.language,
                info.language_probability,
            )
            return segments, info
        except Exception as exc:
            logger.error("Transcription failed: %s", exc, exc_info=True)
            raise CompadreError(
                message=f"Transcription failed: {exc}",
                status_code=500,
                error_type="server_error",
                hint="The audio may be corrupted or in an unsupported format.",
            ) from exc


def _generate_silent_wav(duration_ms: int, sample_rate: int) -> bytes:
    """Generate a minimal silent mono WAV file for warm-up."""
    import io
    import wave

    num_samples = int(sample_rate * (duration_ms / 1000.0))
    pcm_data = b"\x00\x00" * num_samples  # 16-bit silence

    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm_data)
    return buf.getvalue()
