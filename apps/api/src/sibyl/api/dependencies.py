"""FastAPI dependencies for graph operations.

These dependencies provide pre-configured native managers for route handlers,
eliminating repeated boilerplate for client/manager initialization.

Usage:
    @router.get("/entities")
    async def list_entities(
        manager = Depends(get_entity_manager),
    ) -> list[Entity]:
        return await manager.list_all()
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from fastapi import Depends

from sibyl.auth.dependencies import get_current_organization
from sibyl_core.auth import AuthOrganization
from sibyl_core.services import KnowledgeReadService

if TYPE_CHECKING:
    from sibyl.persistence.graph_runtime import ActiveGraphStore as ActiveGraphStoreType
    from sibyl_core.services.graph import (
        EntityManager,
        SurrealGraphClient,
    )
    from sibyl_core.storage import GraphStore


class _ActiveGraphStoreProxy:
    @staticmethod
    def from_client(
        client: SurrealGraphClient,
        group_id: str,
    ) -> ActiveGraphStoreType:
        from sibyl.persistence.graph_runtime import ActiveGraphStore

        return ActiveGraphStore.from_client(client, group_id)

    @staticmethod
    def from_runtime(runtime: Any, group_id: str) -> ActiveGraphStoreType:
        from sibyl.persistence.graph_runtime import ActiveGraphStore

        return ActiveGraphStore.from_runtime(runtime, group_id)


class _GraphReadServiceAdapterProxy:
    def __new__(cls, graph_store: GraphStore) -> KnowledgeReadService:
        from sibyl.persistence.graph_runtime import GraphReadServiceAdapter

        return GraphReadServiceAdapter(graph_store)


ActiveGraphStore = _ActiveGraphStoreProxy
GraphReadServiceAdapter = _GraphReadServiceAdapterProxy


async def get_graph_client() -> SurrealGraphClient:
    from sibyl_core.services.graph_runtime import get_graph_client as _get_graph_client

    return await _get_graph_client()


async def get_entity_manager(
    org: AuthOrganization = Depends(get_current_organization),
) -> EntityManager:
    """Get a native entity manager scoped to the current organization.

    This dependency combines org context resolution with native graph runtime
    initialization.

    Args:
        org: Current organization from auth context (auto-resolved)

    Returns:
        Entity manager configured for the current org's graph

    Example:
        @router.get("/entities")
        async def list_entities(
            manager = Depends(get_entity_manager),
        ) -> list[Entity]:
            return await manager.list_all()
    """
    from sibyl.persistence.graph_runtime import get_entity_graph_runtime

    runtime = await get_entity_graph_runtime(str(org.id))
    return runtime.entity_manager


async def get_graph_store(
    org: AuthOrganization = Depends(get_current_organization),
) -> ActiveGraphStoreType:
    """Get the backend-agnostic graph store on top of the current runtime."""
    from sibyl.persistence.graph_runtime import get_graph_store as get_runtime_graph_store

    return await get_runtime_graph_store(str(org.id))


async def get_knowledge_read_service(
    graph_store: ActiveGraphStoreType = Depends(get_graph_store),
) -> KnowledgeReadService:
    """Get the seam-based graph read service backed by the active runtime."""
    return GraphReadServiceAdapter(graph_store)
