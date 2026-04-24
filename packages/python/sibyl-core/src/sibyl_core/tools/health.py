"""Health check and statistics functions for Sibyl MCP server."""

import time
from typing import Any

from sibyl_core.models.entities import EntityType
from sibyl_core.services import (
    count_entities_by_type,
)
from sibyl_core.services import (
    execute_graph_query as _execute_graph_query,
)
from sibyl_core.services import (
    get_graph_client as _service_get_graph_client,
)
from sibyl_core.services import (
    get_graph_runtime as _service_get_graph_runtime,
)

# Module-level state for uptime tracking
_server_start_time: float | None = None


async def get_graph_client():
    return await _service_get_graph_client()


async def get_graph_runtime(group_id: str):
    return await _service_get_graph_runtime(group_id)


execute_graph_query = _execute_graph_query


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
        await get_graph_client()

        # Test connectivity
        health["graph_connected"] = True

        # Entity counts require org context
        if organization_id:
            runtime = await get_graph_runtime(organization_id)
            entity_manager = runtime.entity_manager

            try:
                counts = await count_entities_by_type(entity_manager)
            except Exception:
                counts = None

            for entity_type in [EntityType.PATTERN, EntityType.RULE, EntityType.EPISODE]:
                if counts is None:
                    health["entity_counts"][entity_type.value] = -1
                else:
                    health["entity_counts"][entity_type.value] = counts.get(entity_type.value, 0)

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
