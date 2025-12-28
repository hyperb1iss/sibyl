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


def create_combined_app(  # noqa: PLR0915
    host: str | None = None, port: int | None = None, *, embed_worker: bool = False
) -> Starlette:
    """Create a combined Starlette app with MCP and REST API.

    Routes:
        /api/*  - FastAPI REST endpoints
        /mcp    - MCP protocol endpoint (streamable HTTP)
        /       - Root redirect to API docs

    Args:
        host: Host to bind to
        port: Port to listen on
        embed_worker: If True, run arq worker in-process (for dev mode)

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
    async def lifespan(_app: Starlette) -> "AsyncGenerator[None]":  # noqa: PLR0915
        """Combined lifespan that initializes MCP session manager."""
        import asyncio
        import contextlib

        log = structlog.get_logger()

        # === Startup Validation ===
        # Check JWT secret when auth is enabled
        jwt_set = bool(settings.jwt_secret.get_secret_value())
        auth_required = settings.mcp_auth_mode == "on" or (
            settings.mcp_auth_mode == "auto" and jwt_set
        )
        if auth_required and not jwt_set:
            log.warning(
                "JWT secret not configured but auth is required",
                hint="Set SIBYL_JWT_SECRET or JWT_SECRET env var",
            )
        elif not jwt_set and not settings.disable_auth:
            log.info(
                "Running without JWT secret - MCP auth disabled",
                hint="Set SIBYL_JWT_SECRET for authenticated access",
            )

        # Test database connectivity (warn-only, don't hard fail)
        db_connected = False
        try:
            from sqlalchemy import text

            from sibyl.db.connection import get_session

            async with get_session() as session:
                await session.execute(text("SELECT 1"))
            log.info("PostgreSQL connected", host=settings.postgres_host)
            db_connected = True
        except Exception as e:
            log.warning("PostgreSQL unavailable at startup", error=str(e))

        # Recover stuck crawl sources (only if DB is connected)
        if db_connected:
            try:
                from sibyl.api.routes.admin import recover_stuck_sources

                await recover_stuck_sources()
            except Exception as e:
                log.warning("Source recovery failed", error=str(e))

        try:
            from sibyl.graph.client import get_graph_client

            client = await get_graph_client()
            if client.is_connected:
                log.info("FalkorDB connected", host=settings.falkordb_host)
        except Exception as e:
            log.warning("FalkorDB unavailable at startup", error=str(e))

        # Initialize Redis pub/sub for cross-pod WebSocket broadcasts
        pubsub_initialized = False
        try:
            from sibyl.api.pubsub import init_pubsub, shutdown_pubsub
            from sibyl.api.websocket import enable_pubsub, local_broadcast

            await init_pubsub(local_broadcast)
            enable_pubsub()
            pubsub_initialized = True
            log.info("WebSocket pub/sub enabled for multi-pod broadcasts")
        except Exception as e:
            log.warning(
                "Redis pub/sub unavailable - WebSocket broadcasts will be local only",
                error=str(e),
            )

        # Optionally start embedded arq worker (dev mode only)
        worker_task = None
        if embed_worker:
            from sibyl.jobs.worker import run_worker_async

            worker_task = asyncio.create_task(run_worker_async())

        # The MCP session manager needs to be started for streamable HTTP
        async with mcp.session_manager.run():
            yield

        # Shutdown pub/sub
        if pubsub_initialized:
            try:
                from sibyl.api.pubsub import shutdown_pubsub
                from sibyl.api.websocket import disable_pubsub

                disable_pubsub()
                await shutdown_pubsub()
            except Exception as e:
                log.warning("Error shutting down pub/sub", error=str(e))

        # Shutdown embedded worker if running
        if worker_task:
            worker_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await worker_task

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
    from sibyl.banner import print_banner
    from sibyl.tools.admin import mark_server_started

    log = structlog.get_logger()

    # Use settings defaults if not specified
    host = host or settings.server_host
    port = port or settings.server_port

    # Print the gorgeous banner
    print_banner(component="server")

    mark_server_started()

    log.info(
        "Starting Sibyl Server",
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


def create_dev_app() -> Starlette:
    """Factory for dev mode.

    Set SIBYL_RUN_WORKER=true to embed the arq worker in-process.
    Note: arq Worker doesn't handle cancellation gracefully, so avoid using
    with --reload. For dev with hot-reload, run worker separately:
        uv run arq sibyl.jobs.WorkerSettings
    """
    import os

    embed_worker = os.getenv("SIBYL_RUN_WORKER", "").lower() in ("true", "1", "yes")
    return create_combined_app(embed_worker=embed_worker)


def main() -> None:
    """Main entry point for CLI."""
    # Default to streamable-http daemon mode
    run_server()


if __name__ == "__main__":
    main()
