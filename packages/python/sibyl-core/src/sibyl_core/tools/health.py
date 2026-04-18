"""Health check and statistics functions for Sibyl MCP server."""

import time
from typing import Any

from sibyl_core.models.entities import EntityType
from sibyl_core.services.legacy_graph import (
    execute_legacy_graph_query,
    get_legacy_graph_client,
    get_legacy_graph_runtime,
)

# Module-level state for uptime tracking
_server_start_time: float | None = None


async def _count_entities(entity_manager: Any, entity_type: EntityType) -> int:
    """Count entities of a type without truncating large orgs."""
    total = 0
    offset = 0
    page_size = 1000

    while True:
        entities = await entity_manager.list_by_type(
            entity_type,
            limit=page_size,
            offset=offset,
        )
        total += len(entities)
        if len(entities) < page_size:
            return total
        offset += page_size


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
        await get_legacy_graph_client()

        # Test connectivity
        health["graph_connected"] = True

        # Entity counts require org context
        if organization_id:
            runtime = await get_legacy_graph_runtime(organization_id)
            entity_manager = runtime.entity_manager

            # Get entity counts
            for entity_type in [EntityType.PATTERN, EntityType.RULE, EntityType.EPISODE]:
                try:
                    health["entity_counts"][entity_type.value] = await _count_entities(
                        entity_manager,
                        entity_type,
                    )
                except Exception:
                    health["entity_counts"][entity_type.value] = -1

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
        data = await execute_legacy_graph_query(
            organization_id,
            """
            MATCH (n)
            WHERE n.entity_type IS NOT NULL
            RETURN n.entity_type as type, count(*) as count
            """,
        )

        stats: dict[str, Any] = {
            "entity_counts": {},
            "total_entities": 0,
        }

        # Initialize all known types to 0
        for entity_type in EntityType:
            stats["entity_counts"][entity_type.value] = 0

        # Fill in actual counts from query
        for row in data:
            if isinstance(row, dict):
                etype = row.get("type")
                count = row.get("count", 0)
            else:
                etype, count = row[0], row[1]

            if etype:
                stats["entity_counts"][etype] = count
                stats["total_entities"] += count

        return stats

    except Exception as e:
        return {"error": str(e), "entity_counts": {}, "total_entities": 0}
