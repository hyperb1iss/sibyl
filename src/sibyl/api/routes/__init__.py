"""API route modules."""

from sibyl.api.routes.admin import router as admin_router
from sibyl.api.routes.entities import router as entities_router
from sibyl.api.routes.graph import router as graph_router
from sibyl.api.routes.search import router as search_router
from sibyl.api.routes.tasks import router as tasks_router

__all__ = ["admin_router", "entities_router", "graph_router", "search_router", "tasks_router"]
