"""FastAPI application factory.

Creates the REST API app that gets mounted alongside MCP.
"""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.routing import WebSocketRoute

from sibyl.api.routes import (
    admin_router,
    crawler_router,
    entities_router,
    graph_router,
    jobs_router,
    rag_router,
    search_router,
    tasks_router,
)
from sibyl.api.websocket import websocket_handler
from sibyl.config import settings

log = structlog.get_logger()


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncGenerator[None, None]:
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

    # CORS for frontend (localhost:3337)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:3337",
            "http://127.0.0.1:3337",
            f"http://localhost:{settings.server_port}",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Register routers
    app.include_router(entities_router)
    app.include_router(tasks_router)
    app.include_router(search_router)
    app.include_router(graph_router)
    app.include_router(admin_router)
    app.include_router(crawler_router)
    app.include_router(rag_router)
    app.include_router(jobs_router)

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

    return app
