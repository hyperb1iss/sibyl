"""Entry point for the Conventions MCP Server daemon."""

import sys

import structlog

from sibyl.config import settings


def configure_logging() -> None:
    """Configure structured logging."""
    structlog.configure(
        processors=[
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            structlog.dev.ConsoleRenderer()
            if sys.stderr.isatty()
            else structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def run_server(
    host: str | None = None,
    port: int | None = None,
    transport: str = "streamable-http",
) -> None:
    """Run the MCP server.

    Args:
        host: Host to bind to (defaults to settings.server_host)
        port: Port to listen on (defaults to settings.server_port)
        transport: Transport type ('streamable-http', 'sse', or 'stdio')
    """
    configure_logging()
    log = structlog.get_logger()

    # Use settings defaults if not specified
    host = host or settings.server_host
    port = port or settings.server_port

    from sibyl.server import create_mcp_server
    from sibyl.tools.admin import mark_server_started

    mark_server_started()

    log.info(
        "Starting Conventions MCP Server",
        version="0.1.0",
        name=settings.server_name,
        transport=transport,
        host=host,
        port=port,
    )

    # Create server with specified host/port
    mcp = create_mcp_server(host=host, port=port)

    # Run with specified transport
    mcp.run(transport=transport)  # type: ignore[arg-type]


def main() -> None:
    """Main entry point for CLI."""
    # Default to streamable-http daemon mode
    run_server()


if __name__ == "__main__":
    main()
