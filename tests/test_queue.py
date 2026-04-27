"""Tests for asyncio.Lock queue behavior in transcription endpoint."""

from __future__ import annotations

import asyncio
import time
from typing import Any
from unittest.mock import MagicMock

from httpx import ASGITransport, AsyncClient

from compadre.exceptions import CompadreError


def _make_wav_bytes(duration_ms: int = 1000) -> bytes:
    """Generate a minimal mono WAV for upload tests."""
    import io
    import wave

    sample_rate = 16000
    num_samples = int(sample_rate * (duration_ms / 1000.0))
    pcm = b"\x00\x00" * num_samples
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm)
    return buf.getvalue()


class TestSequentialProcessing:
    """Two concurrent requests are processed one at a time under asyncio.Lock."""

    async def test_concurrent_requests_processed_sequentially(
        self,
        app: Any,
        auth_headers: dict[str, str],
        mock_engine: MagicMock,
    ) -> None:
        timestamps: list[tuple[str, float]] = []

        def slow_transcribe(*args: object, **kwargs: object) -> tuple[list, object]:
            timestamps.append(("start", time.monotonic()))
            # Sleep to hold the lock
            time.sleep(0.1)
            timestamps.append(("end", time.monotonic()))
            return [], MagicMock(language="pt", language_probability=0.95, duration=0.0)

        mock_engine.transcribe.side_effect = slow_transcribe

        wav = _make_wav_bytes()
        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            r1, r2 = await asyncio.gather(
                client.post(
                    "/v1/audio/transcriptions",
                    files={"file": ("test.wav", wav, "audio/wav")},
                    headers=auth_headers,
                ),
                client.post(
                    "/v1/audio/transcriptions",
                    files={"file": ("test.wav", wav, "audio/wav")},
                    headers=auth_headers,
                ),
            )

        assert r1.status_code == 200
        assert r2.status_code == 200
        # Both calls completed
        assert len(timestamps) == 4
        # Verify label ordering (prevents false pass if lock is removed)
        assert timestamps[0][0] == "start"
        assert timestamps[1][0] == "end"
        assert timestamps[2][0] == "start"
        assert timestamps[3][0] == "end"
        # Second start must be after first end (sequential, not parallel)
        first_end = timestamps[1][1]
        second_start = timestamps[2][1]
        assert second_start >= first_end


class TestQueueDepth:
    """Queue depth increments and decrements correctly."""

    async def test_queue_depth_during_processing(
        self,
        app: Any,
        auth_headers: dict[str, str],
        mock_engine: MagicMock,
    ) -> None:
        depth_during: list[int] = []

        def capture_depth(*args: object, **kwargs: object) -> tuple[list, object]:
            depth_during.append(app.state.queue_depth)
            return [], MagicMock(language="pt", language_probability=0.95, duration=0.0)

        mock_engine.transcribe.side_effect = capture_depth

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/audio/transcriptions",
                files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
                headers=auth_headers,
            )

        assert response.status_code == 200
        # queue_depth is 1 during processing (incremented before lock)
        assert depth_during == [1]
        # queue_depth returns to 0 after completion
        assert app.state.queue_depth == 0

    async def test_queue_depth_decrements_on_error(
        self,
        app: Any,
        auth_headers: dict[str, str],
        mock_engine: MagicMock,
    ) -> None:
        mock_engine.transcribe.side_effect = CompadreError(
            message="Simulated failure",
            status_code=500,
            error_type="server_error",
            hint="test",
        )

        async with AsyncClient(
            transport=ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            response = await client.post(
                "/v1/audio/transcriptions",
                files={"file": ("test.wav", _make_wav_bytes(), "audio/wav")},
                headers=auth_headers,
            )

        assert response.status_code == 500
        # queue_depth returns to 0 even after error (finally block)
        assert app.state.queue_depth == 0
