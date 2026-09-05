"""Structured logging helpers built on ``structlog``."""
from __future__ import annotations

import logging
import sys

import structlog

from .config import get_settings

_configured = False


def configure_logging() -> None:
    """Configure structlog once, honoring ``MCP_KB_LOG_LEVEL``.

    When the server runs over the stdio transport, logs MUST go to stderr so
    they never corrupt the JSON-RPC stream on stdout.
    """
    global _configured
    if _configured:
        return

    level = getattr(logging, get_settings().log_level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stderr, level=level)

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty()),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(level),
        logger_factory=structlog.PrintLoggerFactory(file=sys.stderr),
        cache_logger_on_first_use=True,
    )
    _configured = True


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger for ``name``."""
    configure_logging()
    return structlog.get_logger(name)
