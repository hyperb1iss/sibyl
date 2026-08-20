"""Community clusters for visualization and cluster detail reads."""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sibyl_core.auth.memory_policy import (
    memory_metadata_read_allowed,
    memory_row_project_id,
    private_scope_granted_for,
)
from sibyl_core.models.entities import Entity, RelationshipType
from sibyl_core.services.graph_community_detection import _detect_communities_from_graph
from sibyl_core.services.graph_community_managers import _entity_summary
from sibyl_core.services.graph_community_models import (
    ClusterSummary,
    CommunityConfig,
    DetectedCommunity,
)
from sibyl_core.services.graph_community_selection import (
    CLUSTER_DETAIL_MAX_EDGES,
    CLUSTER_DETAIL_MAX_NODES,
    CLUSTER_SUMMARY_MEMBER_SAMPLE,
    DETECTION_MAX_ENTITIES,
    DETECTION_MAX_RELATIONSHIPS,
    _build_graph_edges_from_snapshot,
    _snapshot_to_networkx,
)
from sibyl_core.services.graph_community_snapshot import (
    _count_int,
    _get_graph_snapshot,
    _get_visible_graph_snapshot,
    _native_rows,
    _reader_cache_key,
)

log = structlog.get_logger()

CLUSTER_CACHE: dict[
    tuple[str, tuple[str, tuple[str, ...], tuple[str, ...] | None]],
    tuple[datetime, list[ClusterSummary]],
] = {}
CLUSTER_CACHE_TTL = timedelta(minutes=5)


async def _native_type_counts_for_ids(
    client: Any,
    organization_id: str,
    member_ids: list[str],
) -> dict[str, int] | None:
    if not member_ids:
        return {}

    rows = await _native_rows(
        client,
        organization_id,
        """
        SELECT entity_type, count() AS member_count
        FROM entity
        WHERE group_id = $group_id
          AND uuid IN $member_ids
        GROUP BY entity_type;
        """,
        member_ids=member_ids,
    )
    if rows is None:
        return None

    type_counts: dict[str, int] = {}
    for row in rows:
        entity_type = row.get("entity_type")
        if not entity_type:
            continue
        type_counts[str(entity_type)] = _count_int(row.get("member_count"))
    return type_counts


async def _native_type_based_clusters(
    client: Any,
    organization_id: str,
) -> list[ClusterSummary] | None:
    rows = await _native_rows(
        client,
        organization_id,
        """
        SELECT entity_type, count() AS member_count
        FROM entity
        WHERE group_id = $group_id
        GROUP BY entity_type;
        """,
    )
    if rows is None:
        return None

    clusters: list[ClusterSummary] = []
    for index, row in enumerate(rows):
        entity_type = str(row.get("entity_type") or "unknown")
        member_count = _count_int(row.get("member_count"))
        member_rows = await _native_rows(
            client,
            organization_id,
            """
            SELECT uuid
            FROM entity
            WHERE group_id = $group_id
              AND entity_type = $entity_type
            ORDER BY updated_at DESC, created_at DESC
            LIMIT $limit;
            """,
            entity_type=entity_type,
            limit=CLUSTER_SUMMARY_MEMBER_SAMPLE,
        )
        if member_rows is None:
            return None
        member_ids = [str(row.get("uuid")) for row in member_rows if row.get("uuid")]
        clusters.append(
            ClusterSummary(
                id=f"type_{entity_type}_{index}",
                member_count=member_count,
                dominant_type=entity_type,
                type_distribution={entity_type: member_count},
                member_ids=member_ids,
                level=0,
            )
        )

    return clusters


async def _native_entities_by_ids(
    client: Any,
    organization_id: str,
    member_ids: list[str],
) -> dict[str, Entity] | None:
    if not member_ids:
        return {}

    rows = await _native_rows(
        client,
        organization_id,
        """
        SELECT *
        FROM entity
        WHERE group_id = $group_id
          AND uuid IN $member_ids
        ORDER BY updated_at DESC, created_at DESC
        LIMIT $limit;
        """,
        member_ids=member_ids,
        limit=len(member_ids),
    )
    if rows is None:
        return None

    from sibyl_core.services.graph_records import entity_from_surreal_row

    entities: dict[str, Entity] = {}
    for row in rows:
        with contextlib.suppress(Exception):
            entity = entity_from_surreal_row(row)
            if entity.id:
                entities[entity.id] = entity
    return entities


async def _native_relationship_edges_between_ids(
    client: Any,
    organization_id: str,
    member_ids: list[str],
    *,
    max_edges: int,
) -> list[dict[str, Any]] | None:
    if not member_ids:
        return []

    rows = await _native_rows(
        client,
        organization_id,
        """
        SELECT source_id, target_id, name
        FROM relates_to
        WHERE group_id = $group_id
          AND source_id IN $member_ids
          AND target_id IN $member_ids
        LIMIT $limit;
        """,
        member_ids=member_ids,
        limit=max_edges,
    )
    if rows is None:
        return None

    edges: list[dict[str, Any]] = []
    for row in rows:
        source_id = str(row.get("source_id") or "")
        target_id = str(row.get("target_id") or "")
        if not source_id or not target_id:
            continue
        edges.append(
            {
                "source": source_id,
                "target": target_id,
                "type": str(row.get("name") or RelationshipType.RELATED_TO.value),
            }
        )
    return edges


async def get_clusters_for_visualization(
    client: Any,
    organization_id: str,
    force_refresh: bool = False,
    *,
    principal_id: str | None = None,
    accessible_projects: set[str] | None = None,
    allowed_memory_scope_keys: set[str] | None = None,
) -> list[ClusterSummary]:
    """Get clusters optimized for bubble visualization.

    Uses caching to avoid expensive community detection on every request.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        force_refresh: Bypass cache and recompute.
        principal_id: Reader the clusters are authorized as.
        accessible_projects: Graph project IDs the reader is a member of.

    Returns:
        List of ClusterSummary objects for visualization.
    """
    cache_key = (
        organization_id,
        _reader_cache_key(principal_id, accessible_projects, allowed_memory_scope_keys),
    )

    # Check cache
    if not force_refresh and cache_key in CLUSTER_CACHE:
        cached_at, clusters = CLUSTER_CACHE[cache_key]
        if datetime.now(UTC) - cached_at < CLUSTER_CACHE_TTL:
            log.debug("cluster_cache_hit", org_id=organization_id, count=len(clusters))
            return clusters

    log.info("cluster_cache_miss", org_id=organization_id)

    snapshot = await _get_visible_graph_snapshot(
        client,
        organization_id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        max_entities=DETECTION_MAX_ENTITIES,
        max_relationships=DETECTION_MAX_RELATIONSHIPS,
    )

    try:
        detected = _detect_communities_from_graph(
            _snapshot_to_networkx(snapshot.entities, snapshot.relationships),
            config=CommunityConfig(
                resolutions=[1.0],  # Single level for now
                min_community_size=2,
                max_levels=1,
                store_in_graph=False,  # Don't persist, just visualize
            ),
            algorithm="louvain",
        )
    except ImportError:
        # Fallback: Group by entity type if networkx not available
        log.warning("networkx_not_available", msg="falling back to type-based clustering")
        detected = []

    if not detected:
        # Fallback: Create pseudo-clusters by entity type
        clusters = await _create_type_based_clusters(client, organization_id, snapshot.entities)
    else:
        # Convert DetectedCommunity to ClusterSummary
        clusters = await _enrich_cluster_summaries(
            client,
            organization_id,
            detected,
            entity_by_id=snapshot.entity_by_id,
        )

    # Cache result
    CLUSTER_CACHE[cache_key] = (datetime.now(UTC), clusters)
    log.info("cluster_cache_updated", org_id=organization_id, count=len(clusters))

    return clusters


async def _create_type_based_clusters(
    client: Any,
    organization_id: str,
    entities: list[Entity] | None = None,
) -> list[ClusterSummary]:
    """Create clusters based on entity type (fallback when no networkx)."""
    if entities is None:
        # No reader-filtered entity list to group, so the aggregate query is
        # the only source; it counts every row in the org and must not be
        # reached from a request that carries a reader.
        native_clusters = await _native_type_based_clusters(client, organization_id)
        if native_clusters is not None:
            return native_clusters

    try:
        if entities is None:
            snapshot = await _get_graph_snapshot(
                client,
                organization_id,
                max_entities=DETECTION_MAX_ENTITIES,
                max_relationships=0,
            )
            entities = snapshot.entities
        grouped_ids: dict[str, list[str]] = {}
        for entity in entities:
            if not entity.id:
                continue
            grouped_ids.setdefault(entity.entity_type.value, []).append(entity.id)

        clusters = []
        for i, (entity_type, member_ids) in enumerate(sorted(grouped_ids.items())):
            if not member_ids:
                continue

            clusters.append(
                ClusterSummary(
                    id=f"type_{entity_type}_{i}",
                    member_count=len(member_ids),
                    dominant_type=entity_type or "unknown",
                    type_distribution={entity_type or "unknown": len(member_ids)},
                    member_ids=member_ids,
                    level=0,
                )
            )

        return clusters

    except Exception as e:
        log.warning("type_based_clusters_failed", error=str(e))
        return []


async def _enrich_cluster_summaries(
    client: Any,
    organization_id: str,
    detected: list[DetectedCommunity],
    *,
    entity_by_id: dict[str, Entity] | None = None,
) -> list[ClusterSummary]:
    """Convert DetectedCommunity to ClusterSummary with type distribution."""
    if entity_by_id is None:
        snapshot = await _get_graph_snapshot(
            client,
            organization_id,
            max_entities=DETECTION_MAX_ENTITIES,
            max_relationships=0,
        )
        entity_by_id = snapshot.entity_by_id
    summaries = []

    for community in detected:
        if not community.member_ids:
            continue

        type_dist = await _native_type_counts_for_ids(client, organization_id, community.member_ids)
        if type_dist is None:
            type_dist = {}
            for member_id in community.member_ids:
                entity = entity_by_id.get(member_id)
                entity_type = entity.entity_type.value if entity is not None else "unknown"
                type_dist[entity_type] = type_dist.get(entity_type, 0) + 1

        # Find dominant type
        dominant = max(type_dist.items(), key=lambda x: x[1])[0] if type_dist else "unknown"

        summaries.append(
            ClusterSummary(
                id=community.id,
                member_count=community.member_count,
                dominant_type=dominant,
                type_distribution=type_dist,
                member_ids=community.member_ids,
                level=community.level,
            )
        )

    return summaries


async def get_cluster_nodes(
    client: Any,
    organization_id: str,
    cluster_id: str,
    *,
    max_nodes: int = CLUSTER_DETAIL_MAX_NODES,
    max_edges: int = CLUSTER_DETAIL_MAX_EDGES,
    principal_id: str | None = None,
    accessible_projects: set[str] | None = None,
    allowed_memory_scope_keys: set[str] | None = None,
) -> dict[str, Any]:
    """Get nodes and edges for a specific cluster.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        cluster_id: Cluster ID to drill into.
        principal_id: Reader the drill-down is authorized as.
        accessible_projects: Graph project IDs the reader is a member of.

    Returns:
        Dict with 'nodes' and 'edges' for the cluster.
    """
    # Get cluster from cache
    clusters = await get_clusters_for_visualization(
        client,
        organization_id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )
    cluster = next((c for c in clusters if c.id == cluster_id), None)

    if not cluster:
        return {"nodes": [], "edges": [], "error": "Cluster not found"}

    member_ids = cluster.member_ids[:max_nodes]
    member_id_set = set(member_ids)
    entity_by_id = await _native_entities_by_ids(client, organization_id, member_ids)
    if entity_by_id is None:
        snapshot = await _get_visible_graph_snapshot(
            client,
            organization_id,
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            max_entities=DETECTION_MAX_ENTITIES,
            max_relationships=DETECTION_MAX_RELATIONSHIPS,
        )
        entity_by_id = snapshot.entity_by_id

    edges = await _native_relationship_edges_between_ids(
        client,
        organization_id,
        member_ids,
        max_edges=max_edges,
    )

    # This is the surface that emits an entity's name and description text, so
    # the rows fetched by id answer to the scope rule again rather than
    # trusting the membership list that selected them.
    nodes = [
        {
            "id": member_id,
            "name": entity.name or member_id[:20],
            "type": entity.entity_type.value,
            "summary": _entity_summary(entity),
        }
        for member_id in member_ids
        if (entity := entity_by_id.get(member_id)) is not None
        and memory_metadata_read_allowed(
            getattr(entity, "metadata", None),
            principal_id=principal_id,
            accessible_projects=accessible_projects,
            allowed_memory_scope_keys=allowed_memory_scope_keys,
            private_scope_granted=private_scope_granted_for(
                allowed_memory_scope_keys, principal_id=principal_id
            ),
            row_project_id=memory_row_project_id(
                getattr(entity, "metadata", None),
                entity_type=getattr(getattr(entity, "entity_type", None), "value", None),
                entity_id=getattr(entity, "id", None),
            ),
        )
    ]
    visible_ids = {node["id"] for node in nodes}
    member_id_set &= visible_ids
    if edges is not None:
        edges = [
            edge
            for edge in edges
            if edge["source"] in visible_ids and edge["target"] in visible_ids
        ]

    if edges is None:
        snapshot = await _get_graph_snapshot(
            client,
            organization_id,
            max_entities=DETECTION_MAX_ENTITIES,
            max_relationships=DETECTION_MAX_RELATIONSHIPS,
        )
        edges = _build_graph_edges_from_snapshot(
            [
                relationship
                for relationship in snapshot.relationships
                if relationship.source_id in member_id_set
                and relationship.target_id in member_id_set
            ],
            member_id_set,
            max_edges=max_edges,
        )

    return {
        "nodes": nodes,
        "edges": edges,
        "cluster_id": cluster_id,
        "member_count": len(nodes),
    }
