"""API route modules."""

from sibyl.api.routes.admin import router as admin_router
from sibyl.api.routes.auth import router as auth_router
from sibyl.api.routes.crawler import router as crawler_router
from sibyl.api.routes.entities import router as entities_router
from sibyl.api.routes.epics import router as epics_router
from sibyl.api.routes.graph import router as graph_router
from sibyl.api.routes.jobs import router as jobs_router
from sibyl.api.routes.metrics import router as metrics_router
from sibyl.api.routes.org_invitations import invitations_router, router as org_invitations_router
from sibyl.api.routes.org_members import router as org_members_router
from sibyl.api.routes.orgs import router as orgs_router
from sibyl.api.routes.rag import router as rag_router
from sibyl.api.routes.search import router as search_router
from sibyl.api.routes.tasks import router as tasks_router
from sibyl.api.routes.users import router as users_router

__all__ = [
    "admin_router",
    "auth_router",
    "crawler_router",
    "entities_router",
    "epics_router",
    "graph_router",
    "jobs_router",
    "invitations_router",
    "metrics_router",
    "org_invitations_router",
    "org_members_router",
    "orgs_router",
    "rag_router",
    "search_router",
    "tasks_router",
    "users_router",
]
