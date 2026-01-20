"""Logging configuration for Sibyl components.

Usage:
    from sibyl_core.logging import configure_logging, get_logger

    configure_logging(service_name="api")
    log = get_logger()
    log.info("Server starting", port=3334)
"""

from __future__ import annotations

import logging
import sys
from typing import Any

import structlog

from sibyl_core.logging.capture import capture_processor
from sibyl_core.logging.formatters import SibylRenderer

# Track if logging has been configured
_configured = False


def configure_logging(
    *,
    service_name: str = "sibyl",
    service_width: int = 7,
    level: str = "INFO",
    colors: bool | None = None,
    json_output: bool = False,
    show_service: bool | None = None,
) -> None:
    """Configure structlog with Sibyl theming.

    Call this once at application startup before any logging.

    Args:
        service_name: Service identifier (api, worker, cli, etc.)
        service_width: Width for service name padding
        level: Minimum log level (DEBUG, INFO, WARNING, ERROR)
        colors: Enable colors (auto-detect TTY/FORCE_COLOR if None)
        json_output: Use JSON output for production/log aggregation
        show_service: Show service prefix (auto-detect CONCURRENTLY env if None)
    """
    import os

    # Auto-detect concurrently: skip service prefix since it provides one
    if show_service is None:
        show_service = os.environ.get("CONCURRENTLY") is None
    global _configured

    # Auto-detect colors
    if colors is None:
        colors = sys.stderr.isatty()

    # Configure stdlib logging first
    _configure_stdlib_logging(level)

    if json_output:
        # JSON mode for production
        renderer: structlog.typing.Processor = structlog.processors.JSONRenderer()
    else:
        # Sibyl themed console output
        renderer = SibylRenderer(
            service_name=service_name,
            service_width=service_width,
            colors=colors,
            show_service=show_service,
        )

    # Processor to inject service name for capture
    def add_service(
        _logger: Any, _method: str, event_dict: structlog.typing.EventDict
    ) -> structlog.typing.EventDict:
        event_dict["_service"] = service_name
        return event_dict

    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="%H:%M:%S"),
            structlog.processors.StackInfoRenderer(),
            # Note: format_exc_info removed - SibylRenderer handles exceptions
            structlog.processors.UnicodeDecoder(),
            add_service,  # Inject service name for capture
            capture_processor,  # Capture to ring buffer for dev introspection
            renderer,
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.PrintLoggerFactory(),
        cache_logger_on_first_use=False,
    )

    _configured = True


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Get a configured logger instance.

    Args:
        name: Optional logger name (usually module __name__)

    Returns:
        Configured structlog BoundLogger
    """
    return structlog.get_logger(name)


def _configure_stdlib_logging(level: str) -> None:
    """Configure stdlib logging and suppress noisy third-party logs."""
    logging.basicConfig(
        format="%(message)s",
        level=getattr(logging, level.upper(), logging.INFO),
        handlers=[logging.StreamHandler()],
    )

    # Suppress known noisy loggers
    noisy_loggers = [
        "uvicorn.access",
        "uvicorn.error",
        "graphiti_core.driver.falkordb_driver",
        "graphiti_core",
        "httpx",
        "httpcore",
        "arq.worker",
        "arq.jobs",
        "mcp",
        "fastmcp",
        "neo4j",
        # Claude SDK - can be noisy during agent termination
        "claude_agent_sdk",
        "claude_code",
    ]
    for logger_name in noisy_loggers:
        logging.getLogger(logger_name).setLevel(logging.WARNING)
