"""API route modules."""

from sibyl.api.routes.admin import router as admin_router
from sibyl.api.routes.auth import router as auth_router
from sibyl.api.routes.crawler import router as crawler_router
from sibyl.api.routes.entities import router as entities_router
from sibyl.api.routes.graph import router as graph_router
from sibyl.api.routes.jobs import router as jobs_router
from sibyl.api.routes.manage import router as manage_router
from sibyl.api.routes.rag import router as rag_router
from sibyl.api.routes.search import router as search_router
from sibyl.api.routes.tasks import router as tasks_router

__all__ = [
    "admin_router",
    "auth_router",
    "crawler_router",
    "entities_router",
    "graph_router",
    "jobs_router",
    "manage_router",
    "rag_router",
    "search_router",
    "tasks_router",
]
