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
    crawler_router,
    entities_router,
    epics_router,
    graph_router,
    invitations_router,
    jobs_router,
    metrics_router,
    org_invitations_router,
    org_members_router,
    orgs_router,
    rag_router,
    search_router,
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


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Pre-warm graph client on startup for fast first requests."""
    log.info("Pre-warming graph client connection...")
    try:
        from sibyl.graph.client import get_graph_client

        await get_graph_client()
        log.info("Graph client ready")
    except Exception as e:
        log.warning("Failed to pre-warm graph client", error=str(e))
    yield


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
    app.include_router(invitations_router)
    app.include_router(rag_router)
    app.include_router(jobs_router)
    app.include_router(metrics_router)
    app.include_router(users_router)

    # WebSocket route for realtime updates
    app.routes.append(WebSocketRoute("/ws", websocket_handler, name="websocket"))

    @app.get("/")
    async def root() -> dict[str, str]:
        """API root - basic info."""
        return {
            "name": "Sibyl API",
            "version": "0.1.0",
            "docs": "/api/docs",
            "websocket": "/api/ws",
        }

    @app.get("/health")
    async def health_check() -> dict[str, str]:
        """Public health check - no auth required.

        Used by load balancers, monitoring, and frontend connection checks.
        For detailed stats, use /admin/health (requires auth).
        """
        return {"status": "healthy"}

    return app
