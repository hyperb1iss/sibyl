"""Entry point for the Sibyl MCP Server daemon.

Hosts both MCP protocol at /mcp and REST API at /api/*.
"""

import os

# Disable Graphiti telemetry before any imports
os.environ.setdefault("GRAPHITI_TELEMETRY_ENABLED", "false")

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from starlette.applications import Starlette
from starlette.routing import Mount

from sibyl.config import settings

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def create_combined_app(host: str | None = None, port: int | None = None) -> Starlette:
    """Create a combined Starlette app with MCP and REST API.

    Routes:
        /api/*  - FastAPI REST endpoints
        /mcp    - MCP protocol endpoint (streamable HTTP)
        /       - Root redirect to API docs

    Args:
        host: Host to bind to
        port: Port to listen on

    Returns:
        Combined Starlette application
    """
    from sibyl.api.app import create_api_app
    from sibyl.server import create_mcp_server

    # Use settings defaults if not specified
    host = host or settings.server_host
    port = port or settings.server_port

    # Create FastAPI app for REST endpoints
    api_app = create_api_app()

    # Create MCP server
    mcp = create_mcp_server(host=host, port=port)

    # Get the MCP ASGI app (streamable HTTP transport)
    mcp_app = mcp.streamable_http_app()

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> "AsyncGenerator[None, None]":
        """Combined lifespan that initializes MCP session manager and background queue."""
        from sibyl.background import init_background_queue, shutdown_background_queue

        # Start background task queue for async enrichment
        await init_background_queue()

        # The MCP session manager needs to be started for streamable HTTP
        async with mcp.session_manager.run():
            yield

        # Shutdown background queue
        await shutdown_background_queue()

    # Create combined app with both mounted
    # Note: streamable_http_app() already routes to /mcp internally
    return Starlette(
        routes=[
            Mount("/api", app=api_app, name="api"),
            Mount("/", app=mcp_app, name="mcp"),
        ],
        lifespan=lifespan,
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
    log = structlog.get_logger()

    # Use settings defaults if not specified
    host = host or settings.server_host
    port = port or settings.server_port

    from sibyl.tools.admin import mark_server_started

    mark_server_started()

    log.info(
        "Starting Sibyl Server",
        version="0.1.0",
        name=settings.server_name,
        transport=transport,
        host=host,
        port=port,
    )

    if transport == "stdio":
        # Legacy stdio mode - MCP only
        from sibyl.server import create_mcp_server

        mcp = create_mcp_server(host=host, port=port)
        mcp.run(transport="stdio")  # type: ignore[arg-type]
    else:
        # HTTP mode - combined app with REST API + MCP
        import uvicorn

        app = create_combined_app(host, port)

        log.info(
            "Server endpoints",
            api=f"http://{host}:{port}/api",
            mcp=f"http://{host}:{port}/mcp",
            docs=f"http://{host}:{port}/api/docs",
        )

        # Configure uvicorn with clean logging
        config = uvicorn.Config(
            app,
            host=host,
            port=port,
            log_level="warning",  # Suppress verbose uvicorn logs
            access_log=False,  # Use our own access logging
        )
        server = uvicorn.Server(config)
        server.run()


def main() -> None:
    """Main entry point for CLI."""
    # Default to streamable-http daemon mode
    run_server()


if __name__ == "__main__":
    main()
