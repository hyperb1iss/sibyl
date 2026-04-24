"""Graph runtime helpers for higher-level service layers."""

from dataclasses import dataclass
from typing import Any

from sibyl_core.graph.client import GraphClient
from sibyl_core.models.entities import EntityType


@dataclass(frozen=True)
class ActiveGraphRuntime:
    """Bound graph collaborators for a single organization."""

    client: Any
    entity_manager: Any
    relationship_manager: Any


async def get_graph_client() -> Any:
    """Return the shared graph client for the active store."""

    from sibyl_core.graph.client import get_graph_client

    return await get_graph_client()


async def get_graph_runtime(group_id: str) -> ActiveGraphRuntime:
    """Bind the active graph managers for a single organization."""

    from sibyl_core.graph.entities import EntityManager
    from sibyl_core.graph.relationships import RelationshipManager

    client = await get_graph_client()
    return ActiveGraphRuntime(
        client=client,
        entity_manager=EntityManager(client, group_id=group_id),
        relationship_manager=RelationshipManager(client, group_id=group_id),
    )


async def count_entities_by_type(
    entity_manager: Any,
    *,
    include_archived: bool = False,
    page_size: int = 1000,
) -> dict[str, int]:
    """Count entities by type without assuming backend-specific aggregations."""

    counts = {entity_type.value: 0 for entity_type in EntityType}
    driver = getattr(entity_manager, "_driver", None)
    group_id = getattr(entity_manager, "_group_id", None)

    if driver is not None and group_id:
        try:
            try:
                from sibyl_core.backends.surreal import SurrealDriver
            except ImportError:
                SurrealDriver = None  # type: ignore[assignment]

            if SurrealDriver is not None and isinstance(driver, SurrealDriver):
                rows = GraphClient.normalize_result(
                    await driver.execute_query(
                        """
                        SELECT entity_type, count() AS cnt
                        FROM entity
                        WHERE group_id = $group_id
                        GROUP BY entity_type;
                        """,
                        group_id=group_id,
                    )
                )
            else:
                rows = GraphClient.normalize_result(
                    await driver.execute_query(
                        """
                        MATCH (n)
                        WHERE n.group_id = $group_id AND n.entity_type IS NOT NULL
                        RETURN n.entity_type AS entity_type, count(*) AS cnt
                        """,
                        group_id=group_id,
                    )
                )

            for row in rows:
                entity_type = row.get("entity_type")
                if entity_type:
                    counts[str(entity_type)] = int(row.get("cnt", 0))
            return counts
        except Exception:
            pass

    offset = 0

    while True:
        entities = await entity_manager.list_all(
            limit=page_size,
            offset=offset,
            include_archived=include_archived,
        )
        if not entities:
            break

        for entity in entities:
            counts[entity.entity_type.value] = counts.get(entity.entity_type.value, 0) + 1

        offset += len(entities)

    return counts


async def execute_graph_query(
    group_id: str,
    query: str,
    **params: Any,
) -> list[dict[str, Any]]:
    """Execute a raw org-scoped graph query and normalize the result."""

    client = await get_graph_client()
    driver = client.client.driver.clone(group_id)
    result = await driver.execute_query(query, **params)
    return client.normalize_result(result)
