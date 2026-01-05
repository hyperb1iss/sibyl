"""Batch graph operations using UNWIND for efficient bulk inserts.

This module provides utilities for creating multiple graph nodes in a single
query using Cypher's UNWIND, significantly reducing database round-trips.

Instead of:
    for entity in entities:
        await node.save(driver)  # N separate queries

Use:
    await batch_create_nodes(driver, org_id, nodes)  # 1 query for all

Performance:
    Sequential: ~100ms per entity = 10s for 100 entities
    UNWIND batch: ~200ms total for 100 entities (50x faster)
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sibyl_core.graph.client import GraphClient

log = structlog.get_logger()


async def batch_create_nodes(
    client: GraphClient,
    organization_id: str,
    nodes: list[dict[str, Any]],
    *,
    label: str = "Entity",
    return_ids: bool = True,
) -> list[str]:
    """Create multiple graph nodes in a single UNWIND query.

    Uses Cypher UNWIND to batch-insert nodes with one database round-trip
    instead of one per node. Dramatically improves bulk insert performance.

    Args:
        client: GraphClient instance.
        organization_id: Org UUID for graph scoping.
        nodes: List of node dictionaries with properties.
            Each dict must have 'uuid' and 'name' keys.
        label: Primary node label (default: "Entity").
        return_ids: Whether to return created UUIDs.

    Returns:
        List of created node UUIDs (if return_ids=True), else empty list.

    Example:
        nodes = [
            {"uuid": "id1", "name": "Task 1", "entity_type": "task", ...},
            {"uuid": "id2", "name": "Task 2", "entity_type": "task", ...},
        ]
        ids = await batch_create_nodes(client, org_id, nodes)
    """
    if not nodes:
        return []

    # Ensure all nodes have required fields
    for i, node in enumerate(nodes):
        if "uuid" not in node:
            raise ValueError(f"Node at index {i} missing required 'uuid' field")
        if "name" not in node:
            raise ValueError(f"Node at index {i} missing required 'name' field")

    # Serialize datetime and nested objects to strings
    serialized_nodes = [_serialize_node(node, organization_id) for node in nodes]

    # Build the UNWIND query
    # UNWIND iterates over the list, CREATE makes one node per iteration
    # All in a single transaction
    return_clause = "RETURN n.uuid AS id" if return_ids else ""
    query = f"""
        UNWIND $nodes AS node
        CREATE (n:{label})
        SET n = node
        {return_clause}
    """

    try:
        result = await client.execute_write_org(query, organization_id, nodes=serialized_nodes)

        if return_ids:
            return [record["id"] for record in result]
        return []

    except Exception as e:
        log.error(
            "batch_create_nodes failed",
            org_id=organization_id,
            node_count=len(nodes),
            error=str(e),
        )
        raise


async def batch_create_episodic_nodes(
    client: GraphClient,
    organization_id: str,
    episodes: list[dict[str, Any]],
    *,
    return_ids: bool = True,
) -> list[str]:
    """Create multiple Episodic nodes in a single UNWIND query.

    Specialized batch create for Episodic nodes (used by add_episode).

    Args:
        client: GraphClient instance.
        organization_id: Org UUID for graph scoping.
        episodes: List of episode dictionaries.
        return_ids: Whether to return created UUIDs.

    Returns:
        List of created episode UUIDs.
    """
    return await batch_create_nodes(
        client,
        organization_id,
        episodes,
        label="Episodic",
        return_ids=return_ids,
    )


async def batch_create_relationships(
    client: GraphClient,
    organization_id: str,
    relationships: list[dict[str, Any]],
    *,
    rel_type: str = "RELATES_TO",
) -> int:
    """Create multiple relationships in a single UNWIND query.

    Args:
        client: GraphClient instance.
        organization_id: Org UUID for graph scoping.
        relationships: List of relationship dicts with:
            - from_uuid: Source node UUID
            - to_uuid: Target node UUID
            - properties: Optional dict of relationship properties
        rel_type: Relationship type (default: "RELATES_TO")

    Returns:
        Number of relationships created.

    Example:
        rels = [
            {"from_uuid": "id1", "to_uuid": "id2", "properties": {"weight": 1.0}},
            {"from_uuid": "id1", "to_uuid": "id3"},
        ]
        count = await batch_create_relationships(client, org_id, rels)
    """
    if not relationships:
        return 0

    # Validate required fields
    for i, rel in enumerate(relationships):
        if "from_uuid" not in rel:
            raise ValueError(f"Relationship at index {i} missing 'from_uuid'")
        if "to_uuid" not in rel:
            raise ValueError(f"Relationship at index {i} missing 'to_uuid'")

    # Normalize relationships - ensure properties dict exists
    normalized = []
    for rel in relationships:
        normalized.append(
            {
                "from_uuid": rel["from_uuid"],
                "to_uuid": rel["to_uuid"],
                "properties": _serialize_properties(rel.get("properties", {})),
            }
        )

    # UNWIND for batch relationship creation
    # MATCH finds both nodes, MERGE creates the relationship
    query = f"""
        UNWIND $rels AS rel
        MATCH (from {{uuid: rel.from_uuid}})
        MATCH (to {{uuid: rel.to_uuid}})
        MERGE (from)-[r:{rel_type}]->(to)
        SET r += rel.properties
        RETURN count(r) AS created
    """

    try:
        result = await client.execute_write_org(query, organization_id, rels=normalized)
        return result[0]["created"] if result else 0

    except Exception as e:
        log.error(
            "batch_create_relationships failed",
            org_id=organization_id,
            rel_count=len(relationships),
            error=str(e),
        )
        raise


async def batch_update_nodes(
    client: GraphClient,
    organization_id: str,
    updates: list[dict[str, Any]],
    *,
    label: str | None = None,
) -> int:
    """Update multiple nodes in a single UNWIND query.

    Args:
        client: GraphClient instance.
        organization_id: Org UUID for graph scoping.
        updates: List of update dicts with:
            - uuid: Node UUID to update
            - properties: Dict of properties to set/update
        label: Optional label filter (only update nodes with this label).

    Returns:
        Number of nodes updated.

    Example:
        updates = [
            {"uuid": "id1", "properties": {"status": "done"}},
            {"uuid": "id2", "properties": {"status": "doing", "priority": "high"}},
        ]
        count = await batch_update_nodes(client, org_id, updates)
    """
    if not updates:
        return 0

    # Validate and serialize
    serialized = []
    for i, update in enumerate(updates):
        if "uuid" not in update:
            raise ValueError(f"Update at index {i} missing 'uuid'")
        if "properties" not in update:
            raise ValueError(f"Update at index {i} missing 'properties'")

        serialized.append(
            {
                "uuid": update["uuid"],
                "properties": _serialize_properties(update["properties"]),
            }
        )

    # Build query with optional label filter
    label_clause = f":{label}" if label else ""
    query = f"""
        UNWIND $updates AS update
        MATCH (n{label_clause} {{uuid: update.uuid}})
        SET n += update.properties
        RETURN count(n) AS updated
    """

    try:
        result = await client.execute_write_org(query, organization_id, updates=serialized)
        return result[0]["updated"] if result else 0

    except Exception as e:
        log.error(
            "batch_update_nodes failed",
            org_id=organization_id,
            update_count=len(updates),
            error=str(e),
        )
        raise


async def batch_delete_nodes(
    client: GraphClient,
    organization_id: str,
    uuids: list[str],
    *,
    label: str | None = None,
    detach: bool = True,
) -> int:
    """Delete multiple nodes in a single UNWIND query.

    Args:
        client: GraphClient instance.
        organization_id: Org UUID for graph scoping.
        uuids: List of node UUIDs to delete.
        label: Optional label filter.
        detach: If True, also delete relationships (DETACH DELETE).

    Returns:
        Number of nodes deleted.
    """
    if not uuids:
        return 0

    label_clause = f":{label}" if label else ""
    delete_clause = "DETACH DELETE n" if detach else "DELETE n"

    query = f"""
        UNWIND $uuids AS uuid
        MATCH (n{label_clause} {{uuid: uuid}})
        {delete_clause}
        RETURN count(*) AS deleted
    """

    try:
        result = await client.execute_write_org(query, organization_id, uuids=uuids)
        return result[0]["deleted"] if result else 0

    except Exception as e:
        log.error(
            "batch_delete_nodes failed",
            org_id=organization_id,
            uuid_count=len(uuids),
            error=str(e),
        )
        raise


def _serialize_node(node: dict[str, Any], group_id: str) -> dict[str, Any]:
    """Serialize a node dict for Cypher parameter passing.

    Args:
        node: Node dictionary with properties.
        group_id: Organization ID to set as group_id.

    Returns:
        Serialized node dict safe for Cypher parameters.
    """
    result: dict[str, Any] = {
        "group_id": group_id,
        "created_at": datetime.now(UTC).isoformat(),
    }

    for key, value in node.items():
        result[key] = _serialize_value(value)

    return result


def _serialize_properties(props: dict[str, Any]) -> dict[str, Any]:
    """Serialize a properties dict for Cypher parameter passing.

    Args:
        props: Properties dictionary.

    Returns:
        Serialized properties safe for Cypher parameters.
    """
    return {key: _serialize_value(value) for key, value in props.items()}


def _serialize_value(value: Any) -> Any:
    """Serialize a single value for Cypher parameter passing.

    Cypher parameters can only contain primitives, lists of primitives,
    or maps of primitives. Nested objects must be JSON-serialized.

    Args:
        value: Value to serialize.

    Returns:
        Serialized value safe for Cypher parameters.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, dict):
        return json.dumps(value)
    if isinstance(value, list):
        # Check if list contains complex objects
        if value and isinstance(value[0], dict):
            return json.dumps(value)
        return value
    if hasattr(value, "value"):  # Enum
        return value.value
    return value
