"""Tests for ``compadre.download`` (Whisper model auto-download).

Lean by design (test-suite review 2026-04-25): per-model variant tests
collapsed; kept the huggingface_hub integration + skip-when-present
short-circuit.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from compadre.download import ensure_whisper_model


def test_ensure_whisper_model_calls_snapshot_download(
    tmp_path: Path,
) -> None:
    """ensure_whisper_model delegates to huggingface_hub.snapshot_download."""
    with patch("huggingface_hub.snapshot_download") as mock_snap:
        mock_snap.return_value = str(tmp_path / "models")
        ensure_whisper_model(model_size="large-v3-turbo", cache_dir=str(tmp_path))
        mock_snap.assert_called_once()
        assert "large-v3-turbo" in str(mock_snap.call_args[1]["repo_id"])


def test_ensure_whisper_model_warns_on_unknown_size(
    tmp_path: Path,
) -> None:
    """Unknown model size logs a warning and skips download."""
    with patch("huggingface_hub.snapshot_download") as mock_snap:
        ensure_whisper_model(model_size="tiny", cache_dir=str(tmp_path))
        mock_snap.assert_not_called()
