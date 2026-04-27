"""Structured logging with pipe-delimited format."""

import logging
import sys
from datetime import UTC, datetime

_LEVEL_NAMES: dict[int, str] = {
    logging.DEBUG: "DEBUG",
    logging.INFO: "INFO ",
    logging.WARNING: "WARN ",
    logging.ERROR: "ERROR",
    logging.CRITICAL: "CRIT ",
}


class PipeFormatter(logging.Formatter):
    """Pipe-delimited log formatter: timestamp | level | component | message."""

    def format(self, record: logging.LogRecord) -> str:
        timestamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        level = _LEVEL_NAMES.get(record.levelno, record.levelname.ljust(5)[:5])
        component = record.name
        message = record.getMessage()
        line = f"{timestamp} | {level} | {component} | {message}"
        if record.exc_info and record.exc_info[1] is not None:
            line += "\n" + self.formatException(record.exc_info)
        if record.stack_info:
            line += "\n" + self.formatStack(record.stack_info)
        return line


def get_logger(component: str) -> logging.Logger:
    """Return a logger with pipe-delimited formatting for the given component."""
    logger = logging.getLogger(component)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(PipeFormatter())
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)
        logger.propagate = False
    return logger


def setup_logging(level: int = logging.INFO) -> None:
    """Configure the root logger with pipe-delimited formatting."""
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        handler = logging.StreamHandler(sys.stdout)
        handler.setFormatter(PipeFormatter())
        root.addHandler(handler)
