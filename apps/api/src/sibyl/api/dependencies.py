"""FastAPI dependencies for graph operations.

These dependencies provide pre-configured managers for route handlers,
eliminating repeated boilerplate for client/manager initialization.

Usage:
    @router.get("/entities")
    async def list_entities(
        manager: EntityManager = Depends(get_entity_manager),
    ) -> list[Entity]:
        return await manager.list_all()
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from fastapi import Depends

from sibyl.auth.dependencies import get_current_organization
from sibyl_core.auth import AuthOrganization
from sibyl_core.services import KnowledgeReadService

if TYPE_CHECKING:
    from sibyl.persistence.graph_runtime import ActiveGraphStore as ActiveGraphStoreType
    from sibyl_core.graph import EntityManager, RelationshipManager
    from sibyl_core.graph.client import GraphClient
    from sibyl_core.storage import GraphStore


class _ActiveGraphStoreProxy:
    @staticmethod
    def from_client(client: GraphClient, group_id: str) -> ActiveGraphStoreType:
        from sibyl.persistence.graph_runtime import ActiveGraphStore

        return ActiveGraphStore.from_client(client, group_id)


class _GraphReadServiceAdapterProxy:
    def __new__(cls, graph_store: GraphStore) -> KnowledgeReadService:
        from sibyl.persistence.graph_runtime import GraphReadServiceAdapter

        return GraphReadServiceAdapter(graph_store)


ActiveGraphStore = _ActiveGraphStoreProxy
GraphReadServiceAdapter = _GraphReadServiceAdapterProxy


async def get_graph_client() -> GraphClient:
    from sibyl_core.graph.client import get_graph_client as _get_graph_client

    return await _get_graph_client()


async def get_graph() -> GraphClient:
    """Get the shared graph client.

    This is a thin wrapper around get_graph_client for use as a FastAPI
    dependency. The client is a singleton, so this is cheap to call.

    Returns:
        GraphClient instance
    """
    return await get_graph_client()


async def get_entity_manager(
    org: AuthOrganization = Depends(get_current_organization),
) -> EntityManager:
    """Get an EntityManager scoped to the current organization.

    This dependency combines org context resolution with EntityManager
    initialization, eliminating the common pattern:

        client = await get_graph_client()
        manager = EntityManager(client, group_id=str(org.id))

    Args:
        org: Current organization from auth context (auto-resolved)

    Returns:
        EntityManager configured for the current org's graph

    Example:
        @router.get("/entities")
        async def list_entities(
            manager: EntityManager = Depends(get_entity_manager),
        ) -> list[Entity]:
            return await manager.list_all()
    """
    from sibyl_core.graph import EntityManager

    client = await get_graph_client()
    return EntityManager(client, group_id=str(org.id))


async def get_relationship_manager(
    org: AuthOrganization = Depends(get_current_organization),
) -> RelationshipManager:
    """Get a RelationshipManager scoped to the current organization.

    Similar to get_entity_manager but for relationship operations.

    Args:
        org: Current organization from auth context (auto-resolved)

    Returns:
        RelationshipManager configured for the current org's graph
    """
    from sibyl_core.graph import RelationshipManager

    client = await get_graph_client()
    return RelationshipManager(client, group_id=str(org.id))


async def get_graph_store(
    org: AuthOrganization = Depends(get_current_organization),
) -> ActiveGraphStoreType:
    """Get the backend-agnostic graph store on top of the current runtime."""
    client = await get_graph_client()
    return ActiveGraphStore.from_client(client, str(org.id))


async def get_knowledge_read_service(
    graph_store: ActiveGraphStoreType = Depends(get_graph_store),
) -> KnowledgeReadService:
    """Get the seam-based graph read service backed by the active runtime."""
    return GraphReadServiceAdapter(graph_store)


async def get_group_id(
    org: AuthOrganization = Depends(get_current_organization),
) -> str:
    """Get the graph group_id (org ID as string) for the current organization.

    Useful when you need the group_id for direct graph operations
    without a full manager.

    Returns:
        Organization ID as string (used as FalkorDB graph namespace)
    """
    return str(org.id)
