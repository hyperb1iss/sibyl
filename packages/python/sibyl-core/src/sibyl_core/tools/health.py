"""Health check and statistics functions for Sibyl MCP server."""

import time
from typing import Any

from sibyl_core.models.entities import EntityType

# Module-level state for uptime tracking
_server_start_time: float | None = None


async def get_graph_client():
    from sibyl_core.services.native_graph import get_native_graph_client

    client = await get_native_graph_client("health")
    await client.connect()
    return client


async def get_graph_runtime(group_id: str):
    from sibyl_core.services.native_graph import get_native_graph_runtime

    return await get_native_graph_runtime(group_id)


async def execute_graph_query(
    group_id: str, query: str, **params: object
) -> list[dict[str, object]]:
    from sibyl_core.services.native_graph import normalize_records

    runtime = await get_graph_runtime(group_id)
    return normalize_records(await runtime.client.execute_query(query, **params))


async def get_health(*, organization_id: str | None = None) -> dict[str, Any]:
    """Get server health status.

    Args:
        organization_id: Organization ID for graph operations. If None, only basic
                        connectivity is checked.
    """
    from sibyl_core.config import settings

    global _server_start_time
    if _server_start_time is None:
        _server_start_time = time.time()

    health: dict[str, Any] = {
        "status": "unknown",
        "server_name": settings.server_name,
        "uptime_seconds": int(time.time() - _server_start_time),
        "graph_connected": False,
        "entity_counts": {},
        "errors": [],
    }

    try:
        if organization_id:
            runtime = await get_graph_runtime(organization_id)
            entity_manager = runtime.entity_manager
            health["graph_connected"] = True

            try:
                counts = await count_entities_by_type(entity_manager)
            except Exception:
                counts = None

            for entity_type in [EntityType.PATTERN, EntityType.RULE, EntityType.EPISODE]:
                if counts is None:
                    health["entity_counts"][entity_type.value] = -1
                else:
                    health["entity_counts"][entity_type.value] = counts.get(entity_type.value, 0)
        else:
            await get_graph_client()
            health["graph_connected"] = True

        health["status"] = "healthy"

    except Exception as e:
        health["status"] = "unhealthy"
        health["errors"].append(str(e))

    return health


async def get_stats(organization_id: str | None = None) -> dict[str, Any]:
    """Get knowledge graph statistics.

    Uses a single aggregation query for performance instead of N separate queries.

    Args:
        organization_id: Organization ID to scope stats to (required).

    Raises:
        ValueError: If organization_id is not provided.
    """
    if not organization_id:
        raise ValueError("organization_id is required - cannot get stats without org context")

    try:
        runtime = await get_graph_runtime(organization_id)
        counts = await count_entities_by_type(runtime.entity_manager)

        stats: dict[str, Any] = {
            "entity_counts": counts,
            "total_entities": 0,
        }

        for count in counts.values():
            stats["total_entities"] += count

        return stats

    except Exception as e:
        return {"error": str(e), "entity_counts": {}, "total_entities": 0}


async def count_entities_by_type(
    entity_manager: Any,
    *,
    include_archived: bool = False,
    page_size: int = 1000,
) -> dict[str, int]:
    native_counter = getattr(entity_manager, "count_by_type", None)
    if callable(native_counter):
        return await native_counter(include_archived=include_archived)

    counts = {entity_type.value: 0 for entity_type in EntityType}
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
            entity_type = getattr(entity, "entity_type", None)
            value = getattr(entity_type, "value", entity_type)
            if value:
                counts[str(value)] = counts.get(str(value), 0) + 1

        offset += len(entities)

    return counts
