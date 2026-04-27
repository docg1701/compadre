"""compadre-keys CLI entry points (Typer app + rendering helpers).

Structured CLI logs (``keys.cli`` component) must travel on stderr so that
stdout stays machine-parseable — ``compadre-keys list-keys --format json | jq``
would otherwise see the log line and fail. This module re-targets the
``keys.cli`` logger to stderr before any command runs; the rest of the
project keeps its default stdout handler.
"""

from __future__ import annotations

import logging
import sys

from compadre.logging import PipeFormatter


def _route_cli_logger_to_stderr() -> None:
    logger = logging.getLogger("keys.cli")
    # Drop any handler that writes to stdout — it would pollute JSON output.
    for existing in list(logger.handlers):
        stream = getattr(existing, "stream", None)
        if stream is sys.stdout:
            logger.removeHandler(existing)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(PipeFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        logger.propagate = False


_route_cli_logger_to_stderr()
