"""Whisper model auto-download for first-time container startup."""

from __future__ import annotations

import os
from pathlib import Path

from compadre.logging import get_logger

logger = get_logger("download")

_WHISPER_MODEL_REPO = "mobiuslabsgmbh/faster-whisper-large-v3-turbo"


def ensure_whisper_model(
    model_size: str = "large-v3-turbo",
    cache_dir: str | None = None,
) -> None:
    """Download the Whisper model from HuggingFace Hub if not cached.

    Mirrors Compadre's explicit download pattern: log progress, fail
    loudly on error, let the caller decide whether to continue.
    faster-whisper would auto-download on first ``WhisperModel()`` call,
    but pre-downloading here gives the operator visibility and lets
    air-gapped deployments bake the model into the image volume.
    """
    if cache_dir is not None:
        # scan-allow: HF_HOME set for huggingface_hub which reads
        # os.environ directly — the library does not accept cache_dir
        # at the API level for snapshot_download() resolution.
        os.environ.setdefault("HF_HOME", cache_dir)

    try:
        from huggingface_hub import snapshot_download
    except ImportError as exc:
        logger.critical("huggingface_hub not installed — cannot download Whisper model")
        raise RuntimeError(
            "huggingface_hub is required for Whisper model download"
        ) from exc

    cache_path = Path(
        cache_dir or os.environ.get("HF_HOME", "~/.cache/huggingface")
    ).expanduser()

    repo_id = _WHISPER_MODEL_REPO if model_size == "large-v3-turbo" else None
    if repo_id is None:
        logger.warning(
            "Unknown whisper model size '%s' — skipping explicit download, "
            "faster-whisper will attempt resolution at load time",
            model_size,
        )
        return

    logger.info(
        "Checking Whisper model cache at %s (repo=%s)",
        cache_path,
        repo_id,
    )

    try:
        downloaded = snapshot_download(
            repo_id=repo_id,
            cache_dir=str(cache_path),
            local_files_only=False,
        )
        logger.info(
            "Whisper model ready at %s",
            downloaded,
        )
    except Exception as exc:
        logger.critical(
            "Failed to download Whisper model from HuggingFace Hub: %s",
            exc,
        )
        raise RuntimeError(f"Whisper model download failed: {exc}") from exc
