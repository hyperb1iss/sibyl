"""Conventions Knowledge Graph MCP Server.

Graphiti-powered knowledge graph providing AI agents access to development
conventions, patterns, templates, and hard-won wisdom.
"""

import logging

import structlog

# Configure logging FIRST before any other modules use structlog
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("uvicorn.error").setLevel(logging.WARNING)
# Suppress noisy "Index already exists" from FalkorDB driver
logging.getLogger("graphiti_core.driver.falkordb_driver").setLevel(logging.WARNING)
# Suppress httpx HTTP request logs (extremely noisy during crawls/embeddings)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

# Configure structlog to print directly (bypasses stdlib double-logging)
structlog.configure(
    processors=[
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="%H:%M:%S"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.dev.ConsoleRenderer(colors=True, pad_event=30),
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.PrintLoggerFactory(),  # Direct print, no stdlib double-logging
    cache_logger_on_first_use=False,
)

# Route stdlib logging through structlog for third-party libs
logging.basicConfig(
    format="%(message)s",
    level=logging.INFO,
    handlers=[logging.StreamHandler()],
)

# Suppress noisy third-party logging
logging.getLogger("arq.worker").setLevel(logging.WARNING)
logging.getLogger("arq.jobs").setLevel(logging.WARNING)
logging.getLogger("mcp").setLevel(logging.WARNING)  # FastMCP session manager spam
logging.getLogger("fastmcp").setLevel(logging.WARNING)

from sibyl.config import Settings  # noqa: E402 - must come after structlog config

__version__ = "0.1.0"
__all__ = ["Settings", "__version__"]
