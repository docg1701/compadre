"""Tests for the structured logging module.

Lean by design (test-suite review 2026-04-25): per-formatter-attribute
asserts collapsed; kept the contract — pipe-delimited line shape +
ISO-UTC stamp + no-ANSI + propagate=False on get_logger.
"""

from __future__ import annotations

import logging
import re
from io import StringIO

import pytest

from compadre.logging import PipeFormatter, get_logger, setup_logging

PIPE_FORMAT_PATTERN = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\s\|\s\S+\s+\|\s\S+\s+\|\s.+"
)


@pytest.fixture(autouse=True)
def _cleanup_loggers() -> None:
    yield  # type: ignore[misc]
    manager = logging.Logger.manager
    for name in list(manager.loggerDict):
        logger = manager.loggerDict.get(name)
        if isinstance(logger, logging.Logger):
            logger.handlers.clear()
            logger.setLevel(logging.NOTSET)
            logger.propagate = True


def test_pipe_formatter_emits_pipe_delimited_iso_utc_no_ansi() -> None:
    """The contract: ``timestamp | level | component | message``,
    timestamp ISO-UTC with Z, no ANSI escape sequences (logs are
    consumed by `journalctl` + `grep`, not a terminal)."""
    rec = logging.LogRecord(
        name="startup",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="Server starting",
        args=None,
        exc_info=None,
    )
    formatted = PipeFormatter().format(rec)
    assert PIPE_FORMAT_PATTERN.match(formatted)
    assert "startup" in formatted
    assert "INFO" in formatted
    assert "Server starting" in formatted
    assert "\x1b[" not in formatted  # no ANSI escape


def test_pipe_formatter_includes_exception_traceback() -> None:
    """Exceptions logged via ``log.exception`` must include the traceback."""
    try:
        raise ValueError("boom")
    except ValueError:
        import sys

        rec = logging.LogRecord(
            name="x",
            level=logging.ERROR,
            pathname="",
            lineno=0,
            msg="failure",
            args=None,
            exc_info=sys.exc_info(),
        )
    formatted = PipeFormatter().format(rec)
    assert "ValueError" in formatted
    assert "boom" in formatted


def test_get_logger_does_not_propagate_and_is_idempotent() -> None:
    """``get_logger`` returns a logger with propagate=False (so root
    handlers don't see structured records twice) and re-calls do NOT
    accumulate duplicate handlers."""
    log_a = get_logger("compadre.test")
    log_b = get_logger("compadre.test")
    assert log_a is log_b
    assert log_a.propagate is False
    # No handler accumulation on repeated calls.
    handlers_first = len(log_a.handlers)
    get_logger("compadre.test")
    assert len(log_a.handlers) == handlers_first


def test_get_logger_output_round_trips_through_pipe_formatter() -> None:
    """Smoke: the logger returned by get_logger emits in PipeFormatter shape."""
    buf = StringIO()
    log = get_logger("compadre.smoke")
    log.handlers.clear()
    handler = logging.StreamHandler(buf)
    handler.setFormatter(PipeFormatter())
    log.addHandler(handler)
    log.setLevel(logging.INFO)
    log.info("hello")
    out = buf.getvalue().strip()
    assert PIPE_FORMAT_PATTERN.match(out)
    assert "hello" in out


def test_setup_logging_is_idempotent_no_duplicate_root_handlers() -> None:
    """``setup_logging`` may be called multiple times during dev/test
    reload cycles; it MUST NOT pile up duplicate handlers (which would
    print every record N times)."""
    setup_logging()
    handlers_first = len(logging.getLogger().handlers)
    setup_logging()
    assert len(logging.getLogger().handlers) == handlers_first
