"""Entry point for the Sibyl MCP Server daemon.

Hosts both MCP protocol at /mcp and REST API at /api/*.
"""

import contextlib
import os
import secrets
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

import structlog
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Mount, Route

from sibyl.config import settings
from sibyl.runtime_services import RuntimeServices
from sibyl_core.observability import telemetry_registry

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator


def _enable_dev_signal_diagnostics() -> None:
    enabled = os.getenv("SIBYL_DEV_DIAGNOSTICS", "").lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return

    import faulthandler
    import signal

    sigusr1 = getattr(signal, "SIGUSR1", None)
    if sigusr1 is None:
        return

    with contextlib.suppress(RuntimeError):
        faulthandler.enable()

    with contextlib.suppress(OSError, RuntimeError, ValueError):
        faulthandler.register(sigusr1, all_threads=True)


_METRICS_MEDIA_TYPE = "text/plain; version=0.0.4; charset=utf-8"


async def _root_metrics(request: Request) -> PlainTextResponse:
    if not _root_metrics_authorized(request):
        return PlainTextResponse("not found\n", status_code=404)
    return PlainTextResponse(
        telemetry_registry().prometheus_text(),
        media_type=_METRICS_MEDIA_TYPE,
    )


def _root_metrics_authorized(request: Request) -> bool:
    token = settings.metrics_scrape_token.get_secret_value()
    if token:
        bearer = request.headers.get("authorization", "")
        candidate = ""
        if bearer.lower().startswith("bearer "):
            candidate = bearer[7:].strip()
        candidate = request.headers.get("x-sibyl-metrics-token", candidate)
        return secrets.compare_digest(candidate, token)
    if settings.environment != "development":
        return False
    client = request.client.host if request.client else ""
    return client in {"127.0.0.1", "::1", "localhost"}


def create_combined_app(
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
    async def lifespan(_app: Starlette) -> "AsyncGenerator[None]":
        """Combined lifespan that initializes MCP session manager."""
        import asyncio
        import contextlib

        log = structlog.get_logger()
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

        runtime_services = RuntimeServices(log=log)
        await runtime_services.startup()

        # Optionally start embedded arq worker (dev mode only)
        worker_task = None
        if embed_worker:
            if coordination_backend == "local":
                log.info("Local queue broker runs in-process; no embedded worker task needed")
            else:
                log.warning("Embedded worker disabled in surreal mode", store=settings.store)

        # The MCP session manager needs to be started for streamable HTTP
        try:
            async with mcp.session_manager.run():
                yield
        finally:
            await runtime_services.shutdown()
            if worker_task:
                worker_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await worker_task

    # Create combined app with both mounted
    # Note: streamable_http_app() already routes to /mcp internally
    return Starlette(
        routes=[
            Route("/metrics", _root_metrics, methods=["GET"]),
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

    _enable_dev_signal_diagnostics()
    embed_worker = os.getenv("SIBYL_RUN_WORKER", "").lower() in ("true", "1", "yes")
    return create_combined_app(embed_worker=embed_worker)


def main() -> None:
    """Main entry point for CLI."""
    # Default to streamable-http daemon mode
    run_server()


if __name__ == "__main__":
    main()
