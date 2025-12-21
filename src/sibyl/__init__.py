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

structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
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
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=False,
)

# Suppress noisy arq logging
logging.getLogger("arq.worker").setLevel(logging.WARNING)
logging.getLogger("arq.jobs").setLevel(logging.INFO)

from sibyl.config import Settings  # noqa: E402 - must come after structlog config

__version__ = "0.1.0"
__all__ = ["Settings", "__version__"]
