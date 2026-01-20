"""Sibyl Unified Logging System.

Provides themed structured logging across all Sibyl components.

Usage:
    from sibyl_core.logging import configure_logging, get_logger
    from sibyl_core.logging.colors import ELECTRIC_PURPLE, NEON_CYAN

    # At application startup
    configure_logging(service_name="api")

    # In modules
    log = get_logger()
    log.info("Server starting", port=3334)
"""

from sibyl_core.logging.capture import LogBuffer, LogEntry, capture_processor
from sibyl_core.logging.colors import (
    ANSI_BOLD,
    ANSI_CORAL,
    ANSI_DIM,
    ANSI_ELECTRIC_PURPLE,
    ANSI_ELECTRIC_YELLOW,
    ANSI_ERROR_RED,
    ANSI_NEON_CYAN,
    ANSI_RESET,
    ANSI_SUCCESS_GREEN,
    CORAL,
    DIM,
    ELECTRIC_PURPLE,
    ELECTRIC_YELLOW,
    ERROR_RED,
    LEVEL_COLORS,
    NEON_CYAN,
    SUCCESS_GREEN,
)
from sibyl_core.logging.config import configure_logging, get_logger
from sibyl_core.logging.formatters import SibylRenderer

__all__ = [
    "ANSI_BOLD",
    "ANSI_CORAL",
    "ANSI_DIM",
    "ANSI_ELECTRIC_PURPLE",
    "ANSI_ELECTRIC_YELLOW",
    "ANSI_ERROR_RED",
    "ANSI_NEON_CYAN",
    "ANSI_RESET",
    "ANSI_SUCCESS_GREEN",
    "CORAL",
    "DIM",
    "ELECTRIC_PURPLE",
    "ELECTRIC_YELLOW",
    "ERROR_RED",
    "LEVEL_COLORS",
    "NEON_CYAN",
    "SUCCESS_GREEN",
    "LogBuffer",
    "LogEntry",
    "SibylRenderer",
    "capture_processor",
    "configure_logging",
    "get_logger",
]
