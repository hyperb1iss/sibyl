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
from sibyl.legacy_postgres_startup import bootstrap_legacy_postgres_support

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
        legacy_runtime = settings.store == "legacy"
        coordination_backend = settings.resolved_coordination_backend

        log.info(
            "coordination_backend_resolved",
            backend=coordination_backend,
            configured=settings.coordination_backend,
            store=settings.store,
        )

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

        fully_surreal = settings.store == "surreal" and settings.auth_store == "surreal"

        if settings.store == "surreal" and settings.auth_store != "surreal":
            log.info(
                "Surreal store mode enabled; bootstrapping remaining PostgreSQL-backed services"
            )

        if not fully_surreal:
            await bootstrap_legacy_postgres_support()

        try:
            from sibyl_core.graph.client import get_graph_client

            client = await get_graph_client()
            if client.is_connected:
                log.info("Graph runtime connected", store=settings.store)
        except Exception as e:
            log.warning("Graph runtime unavailable at startup", store=settings.store, error=str(e))

        broker_initialized = False
        queue_backend = "unknown"
        try:
            from sibyl.coordination.broker import get_broker, get_queue_backend

            queue_backend = get_queue_backend()
            await get_broker().startup()
            broker_initialized = True
            log.info(
                "Coordination broker ready",
                backend=coordination_backend,
                queue_backend=queue_backend,
            )
        except Exception as e:
            log.warning(
                "Coordination broker unavailable",
                backend=coordination_backend,
                queue_backend=queue_backend,
                error=str(e),
            )

        scheduler_initialized = False
        try:
            from sibyl.coordination.scheduler import get_scheduler

            await get_scheduler().startup()
            scheduler_initialized = True
            log.info("Coordination scheduler ready", backend=coordination_backend)
        except Exception as e:
            log.warning(
                "Coordination scheduler unavailable",
                backend=coordination_backend,
                error=str(e),
            )

        # Initialize coordination event bus for WebSocket broadcasts
        pubsub_initialized = False
        try:
            from sibyl.api.pubsub import init_pubsub
            from sibyl.api.websocket import enable_pubsub, local_broadcast

            await init_pubsub(local_broadcast)
            enable_pubsub()
            pubsub_initialized = True
            log.info(
                "Coordination event bus enabled for WebSocket broadcasts",
                backend=coordination_backend,
            )
        except Exception as e:
            log.warning(
                "Coordination event bus unavailable - WebSocket broadcasts will stay direct",
                backend=coordination_backend,
                error=str(e),
            )

        # Initialize entity locks
        locks_initialized = False
        try:
            from sibyl.locks import init_locks

            await init_locks()
            locks_initialized = True
            log.info("Coordination locks enabled", backend=coordination_backend)
        except Exception as e:
            log.warning(
                "Coordination locks unavailable - concurrent updates may conflict",
                backend=coordination_backend,
                error=str(e),
            )

        # Optionally start embedded arq worker (dev mode only)
        worker_task = None
        if embed_worker:
            if legacy_runtime:
                from sibyl.jobs.worker import run_worker_async

                worker_task = asyncio.create_task(run_worker_async())
            elif coordination_backend == "local":
                log.info("Local queue broker runs in-process; no embedded worker task needed")
            else:
                log.warning("Embedded worker disabled in surreal mode", store=settings.store)

        # The MCP session manager needs to be started for streamable HTTP
        async with mcp.session_manager.run():
            yield

        # Shutdown coordination event bus
        if pubsub_initialized:
            try:
                from sibyl.api.pubsub import shutdown_pubsub
                from sibyl.api.websocket import disable_pubsub

                disable_pubsub()
                await shutdown_pubsub()
            except Exception as e:
                log.warning("Error shutting down pub/sub", error=str(e))

        # Shutdown locks
        if locks_initialized:
            try:
                from sibyl.locks import shutdown_locks

                await shutdown_locks()
            except Exception as e:
                log.warning("Error shutting down locks", error=str(e))

        if broker_initialized:
            try:
                from sibyl.coordination.broker import get_broker

                await get_broker().shutdown()
            except Exception as e:
                log.warning("Error shutting down broker", error=str(e))

        if scheduler_initialized:
            try:
                from sibyl.coordination.scheduler import get_scheduler

                await get_scheduler().shutdown()
            except Exception as e:
                log.warning("Error shutting down scheduler", error=str(e))

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
    from sibyl_core.tools.admin import mark_server_started

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
        mcp.run(transport="stdio")
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
