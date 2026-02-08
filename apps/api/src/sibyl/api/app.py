"""FastAPI application factory.

Creates the REST API app that gets mounted alongside MCP.
"""

import asyncio
import time
import uuid
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import WebSocketRoute

from sibyl.api.rate_limit import limiter
from sibyl.api.routes import (
    admin_router,
    agent_messages_router,
    agents_router,
    approvals_router,
    auth_router,
    backups_router,
    crawler_router,
    entities_router,
    epics_router,
    graph_router,
    invitations_router,
    jobs_router,
    logs_router,
    metrics_router,
    org_invitations_router,
    org_members_router,
    orgs_router,
    project_members_router,
    rag_router,
    runners_router,
    sandboxes_router,
    search_router,
    settings_router,
    setup_router,
    tasks_router,
    users_router,
)
from sibyl.api.websocket import websocket_handler
from sibyl.auth.middleware import AuthMiddleware
from sibyl.config import settings

log = structlog.get_logger()


def _sandbox_mode() -> str:
    return str(getattr(settings, "sandbox_mode", "off") or "off").lower()


def _sandbox_enabled() -> bool:
    explicit = getattr(settings, "sandbox_enabled", None)
    if isinstance(explicit, bool):
        return explicit
    return _sandbox_mode() in {"shadow", "enforced"}


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log all HTTP requests with method, path, status, and timing."""

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000

        # Log request details
        log.info(
            "request",
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
            client=request.client.host if request.client else None,
        )
        return response


def _run_migrations_sync() -> None:
    """Run database migrations (synchronous)."""
    import os
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    # In Docker: /app/alembic.ini
    # In dev: find relative to source
    alembic_ini = Path(os.environ.get("ALEMBIC_CONFIG", "/app/alembic.ini"))

    # Fallback for development (source tree)
    if not alembic_ini.exists():
        api_root = Path(__file__).parent.parent.parent
        alembic_ini = api_root / "alembic.ini"

    if not alembic_ini.exists():
        log.warning("alembic.ini not found, skipping migrations", path=str(alembic_ini))
        return

    log.info("Running database migrations...", config=str(alembic_ini))
    alembic_cfg = Config(str(alembic_ini))
    command.upgrade(alembic_cfg, "head")
    log.info("Database migrations complete")


async def _run_migrations() -> None:
    """Run database migrations on startup (async wrapper)."""
    import asyncio

    loop = asyncio.get_event_loop()
    await loop.run_in_executor(None, _run_migrations_sync)


async def _init_sandbox_control_plane(app: FastAPI) -> None:
    """Initialize sandbox controller + dispatcher and start reconcile loop."""
    from sibyl.agents.sandbox_controller import SandboxController
    from sibyl.agents.sandbox_dispatcher import SandboxDispatcher
    from sibyl.db.connection import get_session

    dispatcher = SandboxDispatcher(get_session, enabled=True)
    controller = SandboxController(
        get_session,
        enabled=True,
        sandbox_image=settings.sandbox_default_image,
        reconcile_interval_seconds=settings.sandbox_reconcile_interval_seconds,
        idle_ttl_seconds=settings.sandbox_idle_ttl_seconds,
        max_lifetime_seconds=settings.sandbox_max_lifetime_seconds,
        namespace=settings.sandbox_k8s_namespace,
        pod_prefix=settings.sandbox_pod_prefix,
        server_url=settings.sandbox_server_url or settings.server_url,
        k8s_required=settings.sandbox_k8s_required,
        dispatcher=dispatcher,
    )
    stop_event = asyncio.Event()
    reconcile_task = None
    if settings.sandbox_reconcile_enabled:
        reconcile_task = asyncio.create_task(controller.reconcile_loop(stop_event))

    app.state.sandbox_controller = controller
    app.state.sandbox_dispatcher = dispatcher
    app.state.sandbox_reconcile_stop = stop_event
    app.state.sandbox_reconcile_task = reconcile_task
    log.info("Sandbox control plane initialized", mode=_sandbox_mode())


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Run migrations, pre-warm graph client, and start Redis pub/sub on startup."""
    await _run_migrations()

    log.info("Pre-warming graph client connection...")
    try:
        from sibyl_core.graph.client import get_graph_client

        await get_graph_client()
        log.info("Graph client ready")
    except Exception as e:
        log.warning("Failed to pre-warm graph client", error=str(e))

    log.info("Initializing Redis pub/sub for WebSocket broadcasts...")
    try:
        from sibyl.api.pubsub import init_pubsub, shutdown_pubsub
        from sibyl.api.websocket import enable_pubsub, local_broadcast

        await init_pubsub(local_broadcast)
        enable_pubsub()
        log.info("Redis pub/sub ready")
    except Exception as e:
        log.warning(
            "Failed to initialize Redis pub/sub (worker broadcasts may not work)", error=str(e)
        )

    _app.state.sandbox_controller = None
    _app.state.sandbox_dispatcher = None
    _app.state.sandbox_reconcile_stop = None
    _app.state.sandbox_reconcile_task = None
    if _sandbox_enabled():
        try:
            await _init_sandbox_control_plane(_app)
        except Exception as e:
            log.warning("Failed to initialize sandbox control plane", error=str(e), mode=_sandbox_mode())

    yield

    stop_event = getattr(_app.state, "sandbox_reconcile_stop", None)
    reconcile_task = getattr(_app.state, "sandbox_reconcile_task", None)
    if stop_event is not None:
        stop_event.set()
    if reconcile_task is not None:
        reconcile_task.cancel()
        try:
            await reconcile_task
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.debug("Sandbox reconcile task shutdown error", error=str(e))

    try:
        from sibyl.api.pubsub import shutdown_pubsub
        from sibyl.api.websocket import disable_pubsub

        disable_pubsub()
        await shutdown_pubsub()
    except Exception as e:
        log.debug("Pub/sub shutdown error (expected during fast restarts)", error=str(e))


def create_api_app() -> FastAPI:
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI app with all routes and middleware.
    """
    app = FastAPI(
        title="Sibyl API",
        description="REST API for Sibyl Knowledge Graph",
        version="0.1.0",
        docs_url="/docs",
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        lifespan=lifespan,
    )

    # Rate limiting
    if settings.rate_limit_enabled:
        app.state.limiter = limiter
        app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

    # Global exception handler - sanitize all unhandled exceptions
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch unhandled exceptions and return safe error messages.

        Never expose internal exception details to clients. Log full
        details for debugging, return generic message to client.
        """
        error_id = str(uuid.uuid4())[:8]

        log.error(
            "unhandled_exception",
            error_id=error_id,
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

        return JSONResponse(
            status_code=500,
            content={
                "detail": f"An internal error occurred. Please try again later. (ref: {error_id})"
            },
        )

    # CORS - derive allowed origins from public_url
    cors_origins = [
        settings.public_url.rstrip("/"),
        # Dev fallbacks
        "http://localhost:3337",
        "http://127.0.0.1:3337",
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Auth: decode bearer JWTs (no enforcement by default)
    app.add_middleware(AuthMiddleware)

    # Access logging
    app.add_middleware(AccessLogMiddleware)

    # Register routers
    app.include_router(agents_router)
    app.include_router(agent_messages_router)
    app.include_router(approvals_router)
    app.include_router(backups_router)
    app.include_router(entities_router)
    app.include_router(tasks_router)
    app.include_router(epics_router)
    app.include_router(search_router)
    app.include_router(graph_router)
    app.include_router(admin_router)
    app.include_router(auth_router)
    app.include_router(crawler_router)
    app.include_router(orgs_router)
    app.include_router(org_members_router)
    app.include_router(org_invitations_router)
    app.include_router(project_members_router)
    app.include_router(invitations_router)
    app.include_router(rag_router)
    app.include_router(runners_router)
    app.include_router(sandboxes_router)
    app.include_router(jobs_router)
    app.include_router(logs_router)
    app.include_router(metrics_router)
    app.include_router(settings_router)
    app.include_router(setup_router)
    app.include_router(users_router)

    # WebSocket route for realtime updates
    app.routes.append(WebSocketRoute("/ws", websocket_handler, name="websocket"))

    @app.get("/")
    async def root() -> dict[str, str]:
        """API root - basic info."""
        from sibyl import __version__

        return {
            "name": "Sibyl API",
            "version": __version__,
            "docs": "/api/docs",
            "websocket": "/api/ws",
        }

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        """Public health check - no auth required.

        Used by load balancers, monitoring, and frontend connection checks.
        For detailed stats, use /admin/health (requires auth).
        """
        from sibyl import __version__

        return {"status": "healthy", "version": __version__}

    return app
