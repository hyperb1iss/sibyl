"""FastAPI application factory.

Creates the REST API app that gets mounted alongside MCP.
"""

import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

import structlog
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi.errors import RateLimitExceeded
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.routing import WebSocketRoute

from sibyl.api.errors import (
    REQUEST_ID_HEADER,
    generate_request_id,
    get_request_id,
    http_exception_payload,
    internal_error_payload,
    safe_error_payload,
    validation_error_payload,
)
from sibyl.api.rate_limit import limiter
from sibyl.api.routes import (
    admin_router,
    ai_settings_router,
    auth_router,
    backups_router,
    context_router,
    crawler_router,
    entities_router,
    epics_router,
    experience_router,
    graph_router,
    ingestion_router,
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
    resolve_router,
    search_router,
    session_router,
    settings_router,
    setup_router,
    synthesis_router,
    tasks_router,
    teams_router,
    telemetry_router,
    users_router,
)
from sibyl.api.websocket import websocket_handler
from sibyl.auth.middleware import AuthMiddleware
from sibyl.config import settings
from sibyl.runtime_provenance import get_runtime_provenance
from sibyl.runtime_services import RuntimeServices
from sibyl.services.telemetry import schedule_runtime_rollup_persist
from sibyl_core.observability import telemetry_registry

log = structlog.get_logger()


class AccessLogMiddleware(BaseHTTPMiddleware):
    """Log all HTTP requests with method, path, status, and timing."""

    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        request_id = get_request_id(request)
        request.state.request_id = request_id
        response = await call_next(request)
        duration_ms = (time.perf_counter() - start) * 1000
        response.headers[REQUEST_ID_HEADER] = request_id

        log.info(
            "request",
            request_id=request_id,
            method=request.method,
            path=request.url.path,
            status=response.status_code,
            duration_ms=round(duration_ms, 2),
            client=request.client.host if request.client else None,
        )
        telemetry_registry().record_api_request(
            method=request.method,
            route=_route_label(request),
            status_code=response.status_code,
            duration_ms=duration_ms,
        )
        schedule_runtime_rollup_persist()
        return response


class RequestIdMiddleware(BaseHTTPMiddleware):
    """Attach a request ID to request state, response headers, and structlog."""

    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get(REQUEST_ID_HEADER) or generate_request_id()
        request.state.request_id = request_id
        structlog.contextvars.clear_contextvars()
        structlog.contextvars.bind_contextvars(request_id=request_id)
        try:
            response = await call_next(request)
            response.headers[REQUEST_ID_HEADER] = request_id
            return response
        finally:
            structlog.contextvars.clear_contextvars()


def _route_label(request: Request) -> str:
    route = request.scope.get("route")
    route_path = getattr(route, "path", None)
    if isinstance(route_path, str) and route_path:
        return route_path
    return "/unmatched"


def _cookie_secure() -> bool:
    if settings.cookie_secure is not None:
        return bool(settings.cookie_secure)
    if settings.environment == "production":
        return True
    return settings.server_url.startswith("https://")


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None]:
    """Run migrations, pre-warm graph client, and start coordination backends."""
    runtime_services = RuntimeServices(log=log)
    await runtime_services.startup()
    try:
        yield
    finally:
        await runtime_services.shutdown()


def create_api_app() -> FastAPI:  # noqa: PLR0915
    """Create and configure the FastAPI application.

    Returns:
        Configured FastAPI app with all routes and middleware.
    """
    runtime_provenance = get_runtime_provenance()
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

        @app.exception_handler(RateLimitExceeded)
        async def rate_limit_handler(request: Request, _exc: RateLimitExceeded) -> JSONResponse:
            request_id = get_request_id(request)
            log.warning(
                "rate_limit_exceeded",
                request_id=request_id,
                path=request.url.path,
                method=request.method,
            )
            return JSONResponse(
                status_code=429,
                content=safe_error_payload(
                    error="rate_limited",
                    message="Too many requests.",
                    request_id=request_id,
                    remediation="Wait briefly, then retry the command.",
                ),
            )

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        request_id = get_request_id(request)
        payload = http_exception_payload(exc, request_id)
        log.warning(
            "http_exception",
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            status_code=exc.status_code,
            error=payload["error"],
        )
        return JSONResponse(status_code=exc.status_code, content=payload)

    @app.exception_handler(RequestValidationError)
    async def validation_exception_handler(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        request_id = get_request_id(request)
        payload = validation_error_payload(exc.errors(), request_id=request_id)
        log.warning(
            "request_validation_error",
            request_id=request_id,
            path=request.url.path,
            method=request.method,
        )
        return JSONResponse(status_code=422, content=payload)

    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        """Catch unhandled exceptions and return safe error messages.

        Never expose internal exception details to clients. Log full
        details for debugging, return generic message to client.
        """
        request_id = get_request_id(request)

        log.error(
            "unhandled_exception",
            request_id=request_id,
            path=request.url.path,
            method=request.method,
            error_type=type(exc).__name__,
            error_message=str(exc),
        )

        return JSONResponse(
            status_code=500,
            content=internal_error_payload(request_id),
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
    app.add_middleware(
        SessionMiddleware,
        secret_key=settings.jwt_secret.get_secret_value(),
        same_site="lax",
        https_only=_cookie_secure(),
    )

    # Auth: decode bearer JWTs (no enforcement by default)
    app.add_middleware(AuthMiddleware)

    # Access logging
    app.add_middleware(AccessLogMiddleware)
    app.add_middleware(RequestIdMiddleware)

    # Register routers
    app.include_router(backups_router)
    app.include_router(entities_router)
    app.include_router(tasks_router)
    app.include_router(session_router)
    app.include_router(epics_router)
    app.include_router(experience_router)
    app.include_router(search_router)
    app.include_router(context_router)
    app.include_router(graph_router)
    app.include_router(ingestion_router)
    app.include_router(admin_router)
    app.include_router(ai_settings_router)
    app.include_router(auth_router)
    app.include_router(crawler_router)
    app.include_router(orgs_router)
    app.include_router(org_members_router)
    app.include_router(org_invitations_router)
    app.include_router(project_members_router)
    app.include_router(invitations_router)
    app.include_router(rag_router)
    app.include_router(resolve_router)
    app.include_router(jobs_router)
    app.include_router(logs_router)
    app.include_router(memory_router)
    app.include_router(metrics_router)
    app.include_router(settings_router)
    app.include_router(synthesis_router)
    app.include_router(teams_router)
    app.include_router(telemetry_router)
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
    async def health_check() -> dict[str, Any]:
        """Public liveness check - no auth required.

        Asserts only that the process is up. Used by load balancers,
        monitoring, and frontend connection checks. For readiness (can we
        serve traffic?) use /health/ready; for detailed authed stats use
        /admin/health.
        """
        from sibyl import __version__

        return {
            "status": "healthy",
            "version": __version__,
            "runtime": runtime_provenance,
        }

    @app.get("/health/ready")
    async def readiness_check() -> JSONResponse:
        """Public readiness check - no auth required (probes run pre-auth).

        Returns 200 when the serving dependencies (SurrealDB reachability)
        are healthy, 503 with a structured body otherwise. Cheap by design:
        a connect-only handshake that never touches the per-org query path.
        Wire k8s readinessProbe at this path.
        """
        from sibyl.api.readiness import check_readiness

        report = await check_readiness()
        return JSONResponse(
            status_code=200 if report.ready else 503,
            content=report.as_payload(),
        )

    return app
