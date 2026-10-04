"""Application logging configuration with privacy-safe exception rendering."""

from __future__ import annotations

import structlog


def configure_application_logging() -> None:
    """Keep useful tracebacks without printing request/message frame locals."""
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.StackInfoRenderer(),
            structlog.dev.set_exc_info,
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.dev.ConsoleRenderer(
                exception_formatter=structlog.dev.RichTracebackFormatter(show_locals=False),
            ),
        ],
        context_class=dict,
        wrapper_class=structlog.make_filtering_bound_logger(0),
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )
