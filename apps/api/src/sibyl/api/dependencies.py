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
from sibyl_core.graph import EntityManager, RelationshipManager
from sibyl_core.graph.client import get_graph_client

if TYPE_CHECKING:
    from sibyl.db.models import Organization
    from sibyl_core.graph.client import GraphClient


async def get_graph() -> GraphClient:
    """Get the shared graph client.

    This is a thin wrapper around get_graph_client for use as a FastAPI
    dependency. The client is a singleton, so this is cheap to call.

    Returns:
        GraphClient instance
    """
    return await get_graph_client()


async def get_entity_manager(
    org: Organization = Depends(get_current_organization),
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
    client = await get_graph_client()
    return EntityManager(client, group_id=str(org.id))


async def get_relationship_manager(
    org: Organization = Depends(get_current_organization),
) -> RelationshipManager:
    """Get a RelationshipManager scoped to the current organization.

    Similar to get_entity_manager but for relationship operations.

    Args:
        org: Current organization from auth context (auto-resolved)

    Returns:
        RelationshipManager configured for the current org's graph
    """
    client = await get_graph_client()
    return RelationshipManager(client, group_id=str(org.id))


async def get_group_id(
    org: Organization = Depends(get_current_organization),
) -> str:
    """Get the graph group_id (org ID as string) for the current organization.

    Useful when you need the group_id for direct graph operations
    without a full manager.

    Returns:
        Organization ID as string (used as FalkorDB graph namespace)
    """
    return str(org.id)
