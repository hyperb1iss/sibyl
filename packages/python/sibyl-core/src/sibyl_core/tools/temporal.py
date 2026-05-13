"""Temporal query tools for bi-temporal knowledge graph exploration.

Surreal edge records store bi-temporal metadata:
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

from sibyl_core.tools.responses import TemporalEdge, TemporalResponse

log = structlog.get_logger()

__all__ = ["find_conflicts", "get_entity_history", "temporal_query"]


async def get_graph_runtime(organization_id: str) -> Any:
    from sibyl_core.services.native_graph import get_native_graph_runtime

    return await get_native_graph_runtime(organization_id)


async def temporal_query(
    mode: Literal["history", "timeline", "conflicts"] = "history",
    entity_id: str | None = None,
    as_of: str | None = None,
    include_expired: bool = False,
    limit: int = 50,
    organization_id: str | None = None,
) -> TemporalResponse:
    """Query knowledge graph with temporal awareness.

    Exposes Sibyl's bi-temporal edge model for point-in-time queries,
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

    if mode == "history":
        runtime = await get_graph_runtime(organization_id)
        return await get_entity_history(
            runtime.client,
            organization_id,
            entity_id,
            as_of=as_of_dt,
            include_expired=include_expired,
            limit=limit,
        )
    elif mode == "timeline":
        runtime = await get_graph_runtime(organization_id)
        return await get_entity_timeline(
            runtime.client,
            organization_id,
            entity_id,
            limit=limit,
        )
    elif mode == "conflicts":
        runtime = await get_graph_runtime(organization_id)
        return await find_conflicts(
            runtime.client,
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

    if not hasattr(client, "execute_query"):
        return _temporal_backend_unavailable_response(
            mode="history",
            entity_id=entity_id,
            as_of=as_of,
        )

    try:
        edges = await _load_native_temporal_edges(
            client,
            organization_id=organization_id,
            entity_id=entity_id,
            limit=min(max(limit * 4, 100), 1000),
            conflicts_only=False,
        )
        filtered = _filter_history_edges(edges, as_of=as_of, include_expired=include_expired)
        filtered.sort(key=_created_at_sort_key, reverse=True)
        temporal_edges = filtered[:limit]

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

    if not hasattr(client, "execute_query"):
        return _temporal_backend_unavailable_response(
            mode="timeline",
            entity_id=entity_id,
        )

    try:
        edges = await _load_native_temporal_edges(
            client,
            organization_id=organization_id,
            entity_id=entity_id,
            limit=min(max(limit, 100), 1000),
            conflicts_only=False,
        )
        edges.sort(key=_created_at_sort_key)
        temporal_edges = edges[:limit]

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
    if not hasattr(client, "execute_query"):
        return _temporal_backend_unavailable_response(
            mode="conflicts",
            entity_id=entity_id,
        )

    try:
        temporal_edges = await _load_native_temporal_edges(
            client,
            organization_id=organization_id,
            entity_id=entity_id,
            limit=min(max(limit * 4, 100), 1000),
            conflicts_only=True,
        )
        temporal_edges.sort(
            key=lambda edge: (
                edge.expired_at
                or edge.invalid_at
                or edge.created_at
                or datetime.min.replace(tzinfo=UTC)
            ),
            reverse=True,
        )
        temporal_edges = temporal_edges[:limit]

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


def _created_at_sort_key(edge: TemporalEdge) -> datetime:
    created_at = getattr(edge, "created_at", None)
    if isinstance(created_at, datetime):
        return created_at
    return datetime.min.replace(tzinfo=UTC)


def _filter_history_edges(
    edges: list[TemporalEdge],
    *,
    as_of: datetime | None,
    include_expired: bool,
) -> list[TemporalEdge]:
    filtered: list[TemporalEdge] = []
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


async def _load_native_temporal_edges(
    client: Any,
    *,
    organization_id: str,
    entity_id: str | None,
    limit: int,
    conflicts_only: bool,
) -> list[TemporalEdge]:
    from sibyl_core.services.native_graph import normalize_records

    clauses = ["group_id = $group_id"]
    if entity_id:
        clauses.append("(in.uuid = $entity_id OR out.uuid = $entity_id)")
    if conflicts_only:
        clauses.append("(expired_at IS NOT NONE OR invalid_at IS NOT NONE)")
    where_clause = " AND ".join(clauses)
    order_clause = (
        "expired_at DESC, invalid_at DESC, created_at DESC, uuid DESC"
        if conflicts_only
        else "created_at DESC, uuid DESC"
    )

    rows = normalize_records(
        await client.execute_query(
            f"""
            SELECT uuid AS edge_id,
                   name,
                   fact,
                   in.uuid AS source_id,
                   in.name AS source_name,
                   out.uuid AS target_id,
                   out.name AS target_name,
                   created_at,
                   expired_at,
                   valid_at,
                   invalid_at
            FROM relates_to
            WHERE {where_clause}
            ORDER BY {order_clause}
            LIMIT $limit;
            """,
            group_id=organization_id,
            entity_id=entity_id,
            limit=max(int(limit), 1),
        )
    )
    return _parse_edge_results(rows)


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
