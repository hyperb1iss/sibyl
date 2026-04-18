"""Legacy graph runtime helpers for higher-level service layers."""

from dataclasses import dataclass
from typing import Any

from sibyl_core.graph.client import GraphClient, get_graph_client
from sibyl_core.graph.entities import EntityManager
from sibyl_core.graph.relationships import RelationshipManager


@dataclass(frozen=True)
class LegacyGraphRuntime:
    """Bound legacy graph collaborators for a single organization."""

    client: GraphClient
    entity_manager: EntityManager
    relationship_manager: RelationshipManager


async def get_legacy_graph_client() -> GraphClient:
    """Return the shared legacy graph client."""

    return await get_graph_client()


async def get_legacy_graph_runtime(group_id: str) -> LegacyGraphRuntime:
    """Bind the legacy graph managers for a single organization."""

    client = await get_legacy_graph_client()
    return LegacyGraphRuntime(
        client=client,
        entity_manager=EntityManager(client, group_id=group_id),
        relationship_manager=RelationshipManager(client, group_id=group_id),
    )


async def execute_legacy_graph_query(
    group_id: str,
    query: str,
    **params: Any,
) -> list[dict[str, Any]]:
    """Execute a raw org-scoped legacy graph query and normalize the result."""

    client = await get_legacy_graph_client()
    driver = client.client.driver.clone(group_id)
    result = await driver.execute_query(query, **params)
    return client.normalize_result(result)
