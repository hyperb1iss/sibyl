"""Hierarchical and level-of-detail community graph traversal."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import structlog

from sibyl_core.services.graph_community_detection import _detect_communities_from_graph
from sibyl_core.services.graph_community_managers import (
    _list_all_entities,
    _list_all_relationships,
)
from sibyl_core.services.graph_community_models import (
    GRAPH_RESOLUTION_DETAIL,
    GRAPH_RESOLUTION_OVERVIEW,
    CommunityConfig,
    HierarchicalGraphData,
)
from sibyl_core.services.graph_community_selection import (
    _NOISE_RELATIONSHIP_TYPES,
    DETECTION_MAX_ENTITIES,
    DETECTION_MAX_RELATIONSHIPS,
    _build_cluster_detail_graph_from_snapshot,
    _build_graph_edges_from_snapshot,
    _build_graph_nodes_from_snapshot,
    _build_overview_graph_from_snapshot,
    _focused_entity_ids,
    _graph_totals_from_snapshot,
    _lod_cache_key,
    _snapshot_to_networkx,
)
from sibyl_core.services.graph_community_snapshot import (
    _get_visible_graph_snapshot,
    _reader_cache_key,
)

log = structlog.get_logger()

HIERARCHICAL_CACHE: dict[
    tuple[str, tuple[str, tuple[str, ...], tuple[str, ...] | None]],
    tuple[datetime, dict[str, str], list[dict[str, Any]]],
] = {}
HIERARCHICAL_CACHE_TTL = timedelta(minutes=5)
GRAPH_LOD_CACHE: dict[tuple[Any, ...], tuple[datetime, HierarchicalGraphData]] = {}
GRAPH_LOD_CACHE_TTL = timedelta(minutes=2)


async def _get_graph_totals(
    client: Any,
    organization_id: str,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
    include_neighbors: bool = True,
) -> tuple[int, int]:
    """Get total node and edge counts (no LIMIT) for stats display.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        project_ids: Optional list of project IDs to filter by.
        include_neighbors: If True, include 1-hop neighbors of project entities.

    Returns:
        Tuple of (total_nodes, total_edges) matching the filter criteria.
    """
    # NOTE: include_neighbors is intentionally ignored for totals.
    # Totals reflect the focused subset selected by project filters.
    try:
        entities = await _list_all_entities(client, organization_id)
        relationships = await _list_all_relationships(client, organization_id)
        return _graph_totals_from_snapshot(
            entities,
            relationships,
            project_ids=project_ids,
            entity_types=entity_types,
        )
    except Exception as e:
        log.warning("count_graph_totals_failed", error=str(e))
        return 0, 0


async def _fetch_graph_nodes(
    client: Any,
    organization_id: str,
    node_to_cluster: dict[str, str],
    max_nodes: int,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Fetch nodes with cluster assignments, optionally filtered by project/type."""
    try:
        entities = await _list_all_entities(client, organization_id)
        relationships = await _list_all_relationships(client, organization_id)
        return _build_graph_nodes_from_snapshot(
            entities,
            relationships,
            node_to_cluster,
            max_nodes=max_nodes,
            project_ids=project_ids,
            entity_types=entity_types,
        )
    except Exception as e:
        log.warning("fetch_nodes_failed", error=str(e))
        return [], set()


async def _fetch_graph_edges(
    client: Any,
    organization_id: str,
    node_ids: set[str],
    max_edges: int,
) -> list[dict[str, Any]]:
    """Fetch edges between nodes in our set."""
    if not node_ids:
        return []

    try:
        relationships = await _list_all_relationships(client, organization_id)
        return _build_graph_edges_from_snapshot(
            relationships,
            node_ids,
            max_edges=max_edges,
        )
    except Exception as e:
        log.warning("fetch_edges_failed", error=str(e))
        return []


async def get_hierarchical_graph(
    client: Any,
    organization_id: str,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
    max_nodes: int = 1000,
    max_edges: int = 5000,
    resolution: str = GRAPH_RESOLUTION_DETAIL,
    cluster_id: str | None = None,
    *,
    principal_id: str | None = None,
    accessible_projects: set[str] | None = None,
    allowed_memory_scope_keys: set[str] | None = None,
) -> HierarchicalGraphData:
    """Get graph data with cluster assignments for rich visualization.

    Returns actual nodes and edges (not aggregated bubbles) with each node
    assigned to a cluster based on Louvain community detection.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        project_ids: Optional list of project IDs to filter by.
        entity_types: Optional list of entity types to filter by.
        max_nodes: Maximum nodes to return (will sample if exceeded).
        max_edges: Maximum edges to return.
        principal_id: Reader the graph is authorized as.
        accessible_projects: Graph project IDs the reader is a member of.

    Returns:
        HierarchicalGraphData with nodes, edges, and cluster metadata.
    """
    log.info(
        "get_hierarchical_graph_start",
        org_id=organization_id,
        max_nodes=max_nodes,
        projects=project_ids,
        types=entity_types,
        resolution=resolution,
        cluster_id=cluster_id,
    )

    cache_key = _lod_cache_key(
        organization_id=organization_id,
        project_ids=project_ids,
        entity_types=entity_types,
        resolution=resolution,
        cluster_id=cluster_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
    )
    cached_lod = GRAPH_LOD_CACHE.get(cache_key)
    if cached_lod is not None:
        cached_at, data = cached_lod
        if datetime.now(UTC) - cached_at < GRAPH_LOD_CACHE_TTL:
            log.info(
                "graph_lod_cache_hit",
                org_id=organization_id,
                resolution=resolution,
                cluster_id=cluster_id,
            )
            return data

    # Load the whole graph (within analytic caps) for detection and selection.
    # max_nodes/max_edges are render budgets applied later, never here — capping
    # the snapshot is what produced the disconnected starfield.
    snapshot = await _get_visible_graph_snapshot(
        client,
        organization_id,
        principal_id=principal_id,
        accessible_projects=accessible_projects,
        allowed_memory_scope_keys=allowed_memory_scope_keys,
        max_entities=DETECTION_MAX_ENTITIES,
        max_relationships=DETECTION_MAX_RELATIONSHIPS,
    )
    entities = snapshot.entities
    # Build from structural edges only — drop the MENTIONS hairball so projects
    # and their tasks/memory are the visible structure. Totals reflect this
    # focused graph, not the raw edge count.
    relationships = [
        relationship
        for relationship in snapshot.relationships
        if relationship.relationship_type not in _NOISE_RELATIONSHIP_TYPES
    ]
    totals_focused = _focused_entity_ids(
        entities,
        relationships,
        project_ids=project_ids,
        entity_types=entity_types,
    )
    structural_endpoints: set[str] = set()
    total_edge_count = 0
    for relationship in relationships:
        if relationship.source_id in totals_focused and relationship.target_id in totals_focused:
            total_edge_count += 1
            structural_endpoints.add(relationship.source_id)
            structural_endpoints.add(relationship.target_id)
    total_node_count = len(structural_endpoints)
    log.info(
        "graph_totals_queried",
        total_nodes=total_node_count,
        total_edges=total_edge_count,
        filtered_by_projects=bool(project_ids),
    )

    # Check cache for community detection (expensive operation). Detection runs
    # on the reader's visible subgraph, so its key carries the reader too.
    community_cache_key = (
        organization_id,
        _reader_cache_key(principal_id, accessible_projects, allowed_memory_scope_keys),
    )
    node_to_cluster: dict[str, str] = {}
    clusters_meta: list[dict[str, Any]] = []

    if community_cache_key in HIERARCHICAL_CACHE:
        cached_at, cached_clusters, cached_meta = HIERARCHICAL_CACHE[community_cache_key]
        if datetime.now(UTC) - cached_at < HIERARCHICAL_CACHE_TTL:
            log.info("hierarchical_cache_hit", org_id=organization_id)
            node_to_cluster = cached_clusters
            clusters_meta = cached_meta
        else:
            log.debug("hierarchical_cache_expired", org_id=organization_id)

    # Run community detection if not cached
    if not node_to_cluster:
        try:
            detected = _detect_communities_from_graph(
                _snapshot_to_networkx(entities, relationships),
                config=CommunityConfig(
                    resolutions=[1.0], min_community_size=2, max_levels=1, store_in_graph=False
                ),
                algorithm="louvain",
            )
            if detected:
                for community in detected:
                    for member_id in community.member_ids:
                        node_to_cluster[member_id] = community.id
                clusters_meta = [
                    {"id": c.id, "member_count": c.member_count, "level": c.level} for c in detected
                ]
                log.info(
                    "community_detection_success",
                    clusters=len(detected),
                    assigned_nodes=len(node_to_cluster),
                )
                # Cache the result
                HIERARCHICAL_CACHE[community_cache_key] = (
                    datetime.now(UTC),
                    node_to_cluster,
                    clusters_meta,
                )
            else:
                log.warning("community_detection_empty", msg="no communities detected")
        except ImportError:
            log.warning("networkx_not_available", msg="community detection unavailable")
        except Exception as e:
            log.warning("community_detection_failed", error=str(e))

    if resolution == GRAPH_RESOLUTION_OVERVIEW:
        data = _build_overview_graph_from_snapshot(
            entities,
            relationships,
            node_to_cluster,
            clusters_meta,
            project_ids=project_ids,
            entity_types=entity_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )
    else:
        data = _build_cluster_detail_graph_from_snapshot(
            entities,
            relationships,
            node_to_cluster,
            clusters_meta,
            cluster_id=cluster_id,
            project_ids=project_ids,
            entity_types=entity_types,
            max_nodes=max_nodes,
            max_edges=max_edges,
        )

    if (project_ids or entity_types) and total_node_count == 0 and data.displayed_nodes > 0:
        total_node_count = data.displayed_nodes
    if (project_ids or entity_types) and total_edge_count == 0 and data.displayed_edges > 0:
        total_edge_count = data.displayed_edges

    data.total_nodes = total_node_count
    data.total_edges = total_edge_count
    # Land on the project/task/memory node graph; the domain map is an opt-in lens.
    data.recommended_resolution = GRAPH_RESOLUTION_DETAIL

    log.info(
        "get_hierarchical_graph_complete",
        total_nodes=data.total_nodes,
        total_edges=data.total_edges,
        displayed_nodes=data.displayed_nodes,
        displayed_edges=data.displayed_edges,
        clusters=len(data.clusters),
        resolution=data.resolution,
        cluster_id=cluster_id,
    )

    GRAPH_LOD_CACHE[cache_key] = (datetime.now(UTC), data)
    return data
