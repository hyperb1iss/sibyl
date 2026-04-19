"""Temporal query tools for bi-temporal knowledge graph exploration.

Graphiti stores bi-temporal metadata on edges:
- created_at/expired_at: System time (when edge was created/invalidated in Sibyl)
- valid_at/invalid_at: Real-world time (when fact was/ceased to be true)

This module exposes that temporal information for:
- Point-in-time queries: "What did we know as of March 15?"
- Timeline views: "How has knowledge about X evolved?"
- Conflict detection: "What facts have been superseded?"
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

import structlog

from sibyl_core.services import get_graph_client as _service_get_graph_client
from sibyl_core.tools.responses import TemporalEdge, TemporalResponse

log = structlog.get_logger()

__all__ = ["find_conflicts", "get_entity_history", "temporal_query"]


async def get_graph_client() -> Any:
    return await _service_get_graph_client()


get_legacy_graph_client = get_graph_client


async def temporal_query(
    mode: Literal["history", "timeline", "conflicts"] = "history",
    entity_id: str | None = None,
    as_of: str | None = None,
    include_expired: bool = False,
    limit: int = 50,
    organization_id: str | None = None,
) -> TemporalResponse:
    """Query knowledge graph with temporal awareness.

    Exposes Graphiti's bi-temporal model for point-in-time queries,
    timeline exploration, and conflict detection.

    MODES:
    - history: Show edges for an entity as they existed at a point in time
    - timeline: Show all versions of edges for an entity over time
    - conflicts: Find edges that have been invalidated (superseded facts)

    Args:
        mode: Query mode - history, timeline, or conflicts.
        entity_id: Entity to query (required for history/timeline modes).
        as_of: ISO datetime for point-in-time query (history mode).
               Example: "2025-03-15" or "2025-03-15T10:30:00Z"
        include_expired: Include expired/invalidated edges (default False).
        limit: Maximum edges to return.
        organization_id: Organization context (required).

    Returns:
        TemporalResponse with edges and their temporal metadata.

    Examples:
        # What did we know about entity X in March?
        temporal_query(mode="history", entity_id="...", as_of="2025-03-15")

        # How has knowledge about X evolved?
        temporal_query(mode="timeline", entity_id="...")

        # What facts have been superseded?
        temporal_query(mode="conflicts", limit=20)
    """
    if not organization_id:
        raise ValueError("organization_id is required")

    log.info(
        "temporal_query",
        mode=mode,
        entity_id=entity_id,
        as_of=as_of,
        include_expired=include_expired,
    )

    # Parse as_of date if provided
    as_of_dt: datetime | None = None
    if as_of:
        try:
            as_of_dt = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if as_of_dt.tzinfo is None:
                as_of_dt = as_of_dt.replace(tzinfo=UTC)
        except ValueError as e:
            return TemporalResponse(
                mode=mode,
                entity_id=entity_id,
                edges=[],
                total=0,
                message=f"Invalid as_of date format: {e}. Use ISO format like 2025-03-15",
            )

    client = await get_graph_client()

    if mode == "history":
        return await get_entity_history(
            client,
            organization_id,
            entity_id,
            as_of=as_of_dt,
            include_expired=include_expired,
            limit=limit,
        )
    elif mode == "timeline":
        return await get_entity_timeline(
            client,
            organization_id,
            entity_id,
            limit=limit,
        )
    elif mode == "conflicts":
        return await find_conflicts(
            client,
            organization_id,
            entity_id=entity_id,
            limit=limit,
        )
    else:
        return TemporalResponse(
            mode=mode,
            entity_id=entity_id,
            edges=[],
            total=0,
            message=f"Unknown mode: {mode}. Use history, timeline, or conflicts.",
        )


async def get_entity_history(
    client: Any,
    organization_id: str,
    entity_id: str | None,
    as_of: datetime | None = None,
    include_expired: bool = False,
    limit: int = 50,
) -> TemporalResponse:
    """Get edges for an entity, optionally filtered to a point in time.

    Point-in-time semantics:
    - Edges must have been created before as_of (created_at <= as_of)
    - Edges must not have been expired before as_of (expired_at IS NULL OR expired_at > as_of)
    - For real-world validity, valid_at <= as_of AND (invalid_at IS NULL OR invalid_at > as_of)
    """
    if not entity_id:
        return TemporalResponse(
            mode="history",
            entity_id=None,
            edges=[],
            total=0,
            message="entity_id is required for history mode",
        )

    context = _get_surreal_temporal_context(client, organization_id)
    if context is None:
        return _temporal_backend_unavailable_response(
            mode="history",
            entity_id=entity_id,
            as_of=as_of,
        )

    try:
        driver, edge_ops, node_ops = context
        edges = await edge_ops.get_by_node_uuid(driver, entity_id)
        filtered = _filter_history_edges(edges, as_of=as_of, include_expired=include_expired)
        filtered.sort(key=_created_at_sort_key, reverse=True)
        temporal_edges = await _graphiti_edges_to_temporal_edges(
            driver,
            node_ops,
            filtered[:limit],
        )

        return TemporalResponse(
            mode="history",
            entity_id=entity_id,
            edges=temporal_edges,
            total=len(temporal_edges),
            as_of=as_of,
        )

    except Exception as e:
        log.warning("get_entity_history_failed", error=str(e), entity_id=entity_id)
        return TemporalResponse(
            mode="history",
            entity_id=entity_id,
            edges=[],
            total=0,
            message=f"Query failed: {e}",
        )


async def get_entity_timeline(
    client: Any,
    organization_id: str,
    entity_id: str | None,
    limit: int = 100,
) -> TemporalResponse:
    """Get all edges for an entity over time, including expired ones.

    Shows the evolution of knowledge about an entity.
    """
    if not entity_id:
        return TemporalResponse(
            mode="timeline",
            entity_id=None,
            edges=[],
            total=0,
            message="entity_id is required for timeline mode",
        )

    context = _get_surreal_temporal_context(client, organization_id)
    if context is None:
        return _temporal_backend_unavailable_response(
            mode="timeline",
            entity_id=entity_id,
        )

    try:
        driver, edge_ops, node_ops = context
        edges = await edge_ops.get_by_node_uuid(driver, entity_id)
        edges.sort(key=_created_at_sort_key)
        temporal_edges = await _graphiti_edges_to_temporal_edges(
            driver,
            node_ops,
            edges[:limit],
        )

        return TemporalResponse(
            mode="timeline",
            entity_id=entity_id,
            edges=temporal_edges,
            total=len(temporal_edges),
            message=(
                f"Timeline shows {len(temporal_edges)} edges. "
                "Expired edges indicate superseded information."
            ),
        )

    except Exception as e:
        log.warning("get_entity_timeline_failed", error=str(e), entity_id=entity_id)
        return TemporalResponse(
            mode="timeline",
            entity_id=entity_id,
            edges=[],
            total=0,
            message=f"Query failed: {e}",
        )


async def find_conflicts(
    client: Any,
    organization_id: str,
    entity_id: str | None = None,
    limit: int = 50,
) -> TemporalResponse:
    """Find edges that have been invalidated (superseded facts).

    These represent facts that were once believed true but have been
    updated or contradicted by newer information.

    Conflict indicators:
    - expired_at IS NOT NULL: Edge was invalidated in the system
    - invalid_at IS NOT NULL: Fact is no longer true in real world
    """
    context = _get_surreal_temporal_context(client, organization_id)
    if context is None:
        return _temporal_backend_unavailable_response(
            mode="conflicts",
            entity_id=entity_id,
        )

    try:
        driver, edge_ops, node_ops = context
        edges = await _load_surreal_conflict_edges(
            driver,
            edge_ops,
            organization_id=organization_id,
            entity_id=entity_id,
            limit=limit,
        )
        temporal_edges = await _graphiti_edges_to_temporal_edges(
            driver,
            node_ops,
            edges,
        )

        message = f"Found {len(temporal_edges)} invalidated edges"
        if entity_id:
            message += f" for entity {entity_id}"
        message += ". These facts have been superseded by newer information."

        return TemporalResponse(
            mode="conflicts",
            entity_id=entity_id,
            edges=temporal_edges,
            total=len(temporal_edges),
            message=message,
        )

    except Exception as e:
        log.warning("find_conflicts_failed", error=str(e))
        return TemporalResponse(
            mode="conflicts",
            entity_id=entity_id,
            edges=[],
            total=0,
            message=f"Query failed: {e}",
        )


def _temporal_backend_unavailable_response(
    *,
    mode: Literal["history", "timeline", "conflicts"],
    entity_id: str | None,
    as_of: datetime | None = None,
) -> TemporalResponse:
    return TemporalResponse(
        mode=mode,
        entity_id=entity_id,
        edges=[],
        total=0,
        as_of=as_of,
        message="Temporal queries require a Surreal-backed graph runtime.",
    )


def _get_surreal_temporal_context(client: Any, organization_id: str) -> tuple[Any, Any, Any] | None:
    try:
        from sibyl_core.backends.surreal import SurrealDriver
    except ImportError:
        return None

    base_driver = getattr(getattr(client, "client", None), "driver", None)
    if base_driver is None or not hasattr(base_driver, "clone"):
        return None

    driver = base_driver.clone(organization_id)
    if not isinstance(driver, SurrealDriver):
        return None

    edge_ops = getattr(driver, "entity_edge_ops", None)
    node_ops = getattr(driver, "entity_node_ops", None)
    if edge_ops is None or node_ops is None:
        return None

    return driver, edge_ops, node_ops


def _created_at_sort_key(edge: Any) -> datetime:
    created_at = getattr(edge, "created_at", None)
    if isinstance(created_at, datetime):
        return created_at
    return datetime.min.replace(tzinfo=UTC)


def _filter_history_edges(
    edges: list[Any],
    *,
    as_of: datetime | None,
    include_expired: bool,
) -> list[Any]:
    filtered: list[Any] = []
    for edge in edges:
        created_at = getattr(edge, "created_at", None)
        expired_at = getattr(edge, "expired_at", None)
        valid_at = getattr(edge, "valid_at", None)
        invalid_at = getattr(edge, "invalid_at", None)

        if as_of is not None:
            if isinstance(created_at, datetime) and created_at > as_of:
                continue
            if isinstance(expired_at, datetime) and expired_at <= as_of:
                continue
            if isinstance(valid_at, datetime) and valid_at > as_of:
                continue
            if isinstance(invalid_at, datetime) and invalid_at <= as_of:
                continue
        elif not include_expired and (
            isinstance(expired_at, datetime) or isinstance(invalid_at, datetime)
        ):
            continue

        filtered.append(edge)
    return filtered


async def _load_surreal_conflict_edges(
    driver: Any,
    edge_ops: Any,
    *,
    organization_id: str,
    entity_id: str | None,
    limit: int,
) -> list[Any]:
    if entity_id:
        edges = await edge_ops.get_by_node_uuid(driver, entity_id)
    else:
        edges = []
        cursor: str | None = None
        batch_size = max(limit, 100)
        while True:
            batch = await edge_ops.get_by_group_ids(
                driver,
                [organization_id],
                limit=batch_size,
                uuid_cursor=cursor,
            )
            if not batch:
                break
            edges.extend(batch)
            if len(batch) < batch_size:
                break
            cursor = batch[-1].uuid

    invalidated = [
        edge
        for edge in edges
        if getattr(edge, "expired_at", None) is not None
        or getattr(edge, "invalid_at", None) is not None
    ]
    invalidated.sort(
        key=lambda edge: (
            getattr(edge, "expired_at", None)
            or getattr(edge, "invalid_at", None)
            or _created_at_sort_key(edge)
        ),
        reverse=True,
    )
    return invalidated[:limit]


async def _graphiti_edges_to_temporal_edges(
    driver: Any,
    node_ops: Any,
    edges: list[Any],
) -> list[TemporalEdge]:
    node_ids = {
        node_id
        for edge in edges
        for node_id in (
            getattr(edge, "source_node_uuid", None),
            getattr(edge, "target_node_uuid", None),
        )
        if node_id
    }
    nodes = await node_ops.get_by_uuids(driver, sorted(node_ids)) if node_ids else []
    names = {getattr(node, "uuid", ""): getattr(node, "name", "") for node in nodes}

    return [
        TemporalEdge(
            id=str(getattr(edge, "uuid", "") or ""),
            name=str(getattr(edge, "name", "") or ""),
            source_id=str(getattr(edge, "source_node_uuid", "") or ""),
            source_name=str(names.get(getattr(edge, "source_node_uuid", ""), "")),
            target_id=str(getattr(edge, "target_node_uuid", "") or ""),
            target_name=str(names.get(getattr(edge, "target_node_uuid", ""), "")),
            created_at=getattr(edge, "created_at", None),
            expired_at=getattr(edge, "expired_at", None),
            valid_at=getattr(edge, "valid_at", None),
            invalid_at=getattr(edge, "invalid_at", None),
            fact=getattr(edge, "fact", None),
            is_current=(
                getattr(edge, "expired_at", None) is None
                and getattr(edge, "invalid_at", None) is None
            ),
        )
        for edge in edges
    ]


def _parse_edge_results(
    result: list,
    include_current_flag: bool = True,
) -> list[TemporalEdge]:
    """Parse query results into TemporalEdge objects."""
    edges = []

    for row in result:
        # Handle both dict and tuple results
        if isinstance(row, dict):
            edge_id = row.get("edge_id", "")
            name = row.get("name", "")
            fact = row.get("fact")
            source_id = row.get("source_id", "")
            source_name = row.get("source_name", "")
            target_id = row.get("target_id", "")
            target_name = row.get("target_name", "")
            created_at = row.get("created_at")
            expired_at = row.get("expired_at")
            valid_at = row.get("valid_at")
            invalid_at = row.get("invalid_at")
        else:
            # Tuple/list result
            edge_id = row[0] if len(row) > 0 else ""
            name = row[1] if len(row) > 1 else ""
            fact = row[2] if len(row) > 2 else None
            source_id = row[3] if len(row) > 3 else ""
            source_name = row[4] if len(row) > 4 else ""
            target_id = row[5] if len(row) > 5 else ""
            target_name = row[6] if len(row) > 6 else ""
            created_at = row[7] if len(row) > 7 else None
            expired_at = row[8] if len(row) > 8 else None
            valid_at = row[9] if len(row) > 9 else None
            invalid_at = row[10] if len(row) > 10 else None

        # Parse datetime strings
        created_at = _parse_datetime(created_at)
        expired_at = _parse_datetime(expired_at)
        valid_at = _parse_datetime(valid_at)
        invalid_at = _parse_datetime(invalid_at)

        # Determine if edge is current (not expired/invalidated)
        is_current = expired_at is None and invalid_at is None

        edges.append(
            TemporalEdge(
                id=str(edge_id) if edge_id else "",
                name=str(name) if name else "",
                source_id=str(source_id) if source_id else "",
                source_name=str(source_name) if source_name else "",
                target_id=str(target_id) if target_id else "",
                target_name=str(target_name) if target_name else "",
                created_at=created_at,
                expired_at=expired_at,
                valid_at=valid_at,
                invalid_at=invalid_at,
                fact=str(fact) if fact else None,
                is_current=is_current if include_current_flag else True,
            )
        )

    return edges


def _parse_datetime(value: str | datetime | None) -> datetime | None:
    """Parse a datetime value from string or datetime."""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            # Handle various ISO formats
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=UTC)
            return dt
        except (ValueError, TypeError):
            return None
    return None
