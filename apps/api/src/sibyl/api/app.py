"""FastAPI application factory.

Creates the REST API app that gets mounted alongside MCP.
"""

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
    auth_router,
    backups_router,
    context_router,
    crawler_router,
    entities_router,
    epics_router,
    graph_router,
    invitations_router,
    jobs_router,
    logs_router,
    memory_router,
    metrics_router,
    org_invitations_router,
    org_members_router,
    orgs_router,
    project_members_router,
    rag_router,
    search_router,
    session_router,
    settings_router,
    setup_router,
    tasks_router,
    users_router,
)
from sibyl.api.websocket import websocket_handler
from sibyl.auth.middleware import AuthMiddleware
from sibyl.config import settings

log = structlog.get_logger()


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


async def _bootstrap_surreal_runtime_schemas() -> bool:
    from sibyl.surreal_runtime_startup import bootstrap_surreal_runtime_schemas

    return await bootstrap_surreal_runtime_schemas()


async def _load_runtime_settings_from_db() -> list[str]:
    from sibyl.services.settings import load_runtime_settings_from_db

    return await load_runtime_settings_from_db()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:  # noqa: PLR0915
    """Run migrations, pre-warm graph client, and start coordination backends."""
    coordination_backend = settings.resolved_coordination_backend

    await _bootstrap_surreal_runtime_schemas()
    await _load_runtime_settings_from_db()

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
            "Failed to initialize coordination broker",
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
            "Failed to initialize coordination scheduler",
            backend=coordination_backend,
            error=str(e),
        )

    pubsub_initialized = False
    try:
        from sibyl.api.pubsub import init_pubsub
        from sibyl.api.websocket import enable_pubsub, local_broadcast

        await init_pubsub(local_broadcast)
        enable_pubsub()
        pubsub_initialized = True
        log.info("Coordination event bus ready", backend=coordination_backend)
    except Exception as e:
        log.warning(
            "Failed to initialize coordination event bus",
            backend=coordination_backend,
            error=str(e),
        )

    locks_initialized = False
    try:
        from sibyl.locks import init_locks

        await init_locks()
        locks_initialized = True
        log.info("Coordination locks ready", backend=coordination_backend)
    except Exception as e:
        log.warning(
            "Failed to initialize coordination locks",
            backend=coordination_backend,
            error=str(e),
        )

    yield

    try:
        from sibyl.persistence.surreal.auth import close_shared_surreal_auth_client
        from sibyl.persistence.surreal.content import close_shared_surreal_content_client
        from sibyl_core.services.surreal_content import (
            close_shared_surreal_content_client as close_core_surreal_content_client,
        )

        await close_shared_surreal_auth_client()
        await close_shared_surreal_content_client()
        await close_core_surreal_content_client()
    except Exception as e:
        log.debug("Shared Surreal client shutdown error", error=str(e))

    if pubsub_initialized:
        try:
            from sibyl.api.pubsub import shutdown_pubsub
            from sibyl.api.websocket import disable_pubsub

            disable_pubsub()
            await shutdown_pubsub()
        except Exception as e:
            log.debug("Pub/sub shutdown error (expected during fast restarts)", error=str(e))

    if locks_initialized:
        try:
            from sibyl.locks import shutdown_locks

            await shutdown_locks()
        except Exception as e:
            log.debug("Lock shutdown error (expected during fast restarts)", error=str(e))

    if broker_initialized:
        try:
            from sibyl.coordination.broker import get_broker

            await get_broker().shutdown()
        except Exception as e:
            log.debug("Broker shutdown error (expected during fast restarts)", error=str(e))

    if scheduler_initialized:
        try:
            from sibyl.coordination.scheduler import get_scheduler

            await get_scheduler().shutdown()
        except Exception as e:
            log.debug("Scheduler shutdown error (expected during fast restarts)", error=str(e))


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
    app.include_router(backups_router)
    app.include_router(entities_router)
    app.include_router(tasks_router)
    app.include_router(session_router)
    app.include_router(epics_router)
    app.include_router(search_router)
    app.include_router(context_router)
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
    app.include_router(jobs_router)
    app.include_router(logs_router)
    app.include_router(memory_router)
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
