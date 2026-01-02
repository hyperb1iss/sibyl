"""Community detection using Leiden/Louvain algorithm.

Detects hierarchical communities in the knowledge graph for
GraphRAG-style retrieval and summarization.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sibyl_core.graph.client import GraphClient

log = structlog.get_logger()

# =============================================================================
# Cluster Cache for Visualization
# =============================================================================

CLUSTER_CACHE: dict[str, tuple[datetime, list[ClusterSummary]]] = {}
CLUSTER_CACHE_TTL = timedelta(minutes=5)

# Cache for hierarchical graph community detection (expensive operation)
HIERARCHICAL_CACHE: dict[str, tuple[datetime, dict[str, str], list[dict]]] = {}
HIERARCHICAL_CACHE_TTL = timedelta(minutes=5)


@dataclass
class ClusterSummary:
    """Lightweight cluster summary for visualization.

    Attributes:
        id: Cluster identifier.
        member_count: Number of entities in cluster.
        dominant_type: Most common entity type.
        type_distribution: Entity type -> count mapping.
        member_ids: List of member entity IDs.
        level: Hierarchy level (0 = finest).
    """

    id: str
    member_count: int
    dominant_type: str
    type_distribution: dict[str, int]
    member_ids: list[str]
    level: int = 0


async def get_clusters_for_visualization(
    client: GraphClient,
    organization_id: str,
    force_refresh: bool = False,
) -> list[ClusterSummary]:
    """Get clusters optimized for bubble visualization.

    Uses caching to avoid expensive community detection on every request.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        force_refresh: Bypass cache and recompute.

    Returns:
        List of ClusterSummary objects for visualization.
    """
    cache_key = organization_id

    # Check cache
    if not force_refresh and cache_key in CLUSTER_CACHE:
        cached_at, clusters = CLUSTER_CACHE[cache_key]
        if datetime.now(UTC) - cached_at < CLUSTER_CACHE_TTL:
            log.debug("cluster_cache_hit", org_id=organization_id, count=len(clusters))
            return clusters

    log.info("cluster_cache_miss", org_id=organization_id)

    # Run community detection
    try:
        detected = await detect_communities(
            client,
            organization_id,
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
        clusters = await _create_type_based_clusters(client, organization_id)
    else:
        # Convert DetectedCommunity to ClusterSummary
        clusters = await _enrich_cluster_summaries(client, organization_id, detected)

    # Cache result
    CLUSTER_CACHE[cache_key] = (datetime.now(UTC), clusters)
    log.info("cluster_cache_updated", org_id=organization_id, count=len(clusters))

    return clusters


async def _create_type_based_clusters(
    client: GraphClient,
    organization_id: str,
) -> list[ClusterSummary]:
    """Create clusters based on entity type (fallback when no networkx)."""
    query = """
    MATCH (n)
    WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
    RETURN n.entity_type AS type, collect(n.uuid) AS ids
    """

    try:
        result = await client.execute_read_org(query, organization_id, group_id=organization_id)
        clusters = []

        for i, record in enumerate(result):
            if isinstance(record, (list, tuple)):
                entity_type = record[0] if len(record) > 0 else "unknown"
                member_ids = record[1] if len(record) > 1 else []
            else:
                entity_type = record.get("type", "unknown")
                member_ids = record.get("ids", [])

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
    client: GraphClient,
    organization_id: str,
    detected: list[DetectedCommunity],
) -> list[ClusterSummary]:
    """Convert DetectedCommunity to ClusterSummary with type distribution."""
    summaries = []

    for community in detected:
        if not community.member_ids:
            continue

        # Query type distribution for this cluster's members
        # Use COALESCE to try entity_type first, then extract from labels array
        query = """
        MATCH (n)
        WHERE n.uuid IN $ids
        WITH n,
             CASE
                 WHEN n.entity_type IS NOT NULL THEN n.entity_type
                 WHEN n.labels IS NOT NULL AND size(n.labels) > 1 THEN n.labels[1]
                 ELSE 'unknown'
             END AS resolved_type
        RETURN toLower(resolved_type) AS type, count(*) AS cnt
        """

        type_dist: dict[str, int] = {}
        try:
            result = await client.execute_read_org(query, organization_id, ids=community.member_ids)
            for record in result:
                if isinstance(record, (list, tuple)):
                    t = record[0] if len(record) > 0 else "unknown"
                    c = record[1] if len(record) > 1 else 0
                else:
                    t = record.get("type", "unknown")
                    c = record.get("cnt", 0)
                type_dist[t or "unknown"] = c
        except Exception:
            type_dist = {"unknown": len(community.member_ids)}

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
    client: GraphClient,
    organization_id: str,
    cluster_id: str,
) -> dict[str, Any]:
    """Get nodes and edges for a specific cluster.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        cluster_id: Cluster ID to drill into.

    Returns:
        Dict with 'nodes' and 'edges' for the cluster.
    """
    # Get cluster from cache
    clusters = await get_clusters_for_visualization(client, organization_id)
    cluster = next((c for c in clusters if c.id == cluster_id), None)

    if not cluster:
        return {"nodes": [], "edges": [], "error": "Cluster not found"}

    member_ids = cluster.member_ids

    # Get nodes
    node_query = """
    MATCH (n)
    WHERE n.uuid IN $ids
    RETURN n.uuid AS id, n.name AS name, n.entity_type AS type, n.summary AS summary
    """

    nodes = []
    try:
        result = await client.execute_read_org(node_query, organization_id, ids=member_ids)
        for record in result:
            if isinstance(record, (list, tuple)):
                nodes.append(
                    {
                        "id": record[0],
                        "name": record[1] or record[0][:20],
                        "type": record[2] or "unknown",
                        "summary": record[3] or "",
                    }
                )
            else:
                nodes.append(
                    {
                        "id": record.get("id"),
                        "name": record.get("name") or record.get("id", "")[:20],
                        "type": record.get("type", "unknown"),
                        "summary": record.get("summary", ""),
                    }
                )
    except Exception as e:
        log.warning("get_cluster_nodes_failed", cluster_id=cluster_id, error=str(e))

    # Get edges within cluster
    edge_query = """
    MATCH (a)-[r]->(b)
    WHERE a.uuid IN $ids AND b.uuid IN $ids
    RETURN a.uuid AS source, b.uuid AS target, type(r) AS rel_type
    """

    edges = []
    try:
        result = await client.execute_read_org(edge_query, organization_id, ids=member_ids)
        for record in result:
            if isinstance(record, (list, tuple)):
                edges.append(
                    {
                        "source": record[0],
                        "target": record[1],
                        "type": record[2] or "RELATED",
                    }
                )
            else:
                edges.append(
                    {
                        "source": record.get("source"),
                        "target": record.get("target"),
                        "type": record.get("rel_type", "RELATED"),
                    }
                )
    except Exception as e:
        log.warning("get_cluster_edges_failed", cluster_id=cluster_id, error=str(e))

    return {
        "nodes": nodes,
        "edges": edges,
        "cluster_id": cluster_id,
        "member_count": len(nodes),
    }


def invalidate_cluster_cache(organization_id: str | None = None) -> None:
    """Invalidate cluster cache for an organization or all.

    Args:
        organization_id: Specific org to invalidate, or None for all.
    """
    if organization_id:
        CLUSTER_CACHE.pop(organization_id, None)
        log.debug("cluster_cache_invalidated", org_id=organization_id)
    else:
        CLUSTER_CACHE.clear()
        log.debug("cluster_cache_cleared")


def invalidate_hierarchical_cache(organization_id: str | None = None) -> None:
    """Invalidate hierarchical graph cache for an organization or all.

    Args:
        organization_id: Specific org to invalidate, or None for all.
    """
    if organization_id:
        HIERARCHICAL_CACHE.pop(organization_id, None)
        log.debug("hierarchical_cache_invalidated", org_id=organization_id)
    else:
        HIERARCHICAL_CACHE.clear()
        log.debug("hierarchical_cache_cleared")


# =============================================================================
# Hierarchical Graph Data for Rich Visualization
# =============================================================================


@dataclass
class HierarchicalGraphData:
    """Graph data with cluster assignments for rich visualization.

    This structure enables frontend to:
    - Render all nodes with edges (real graph structure)
    - Color nodes by cluster membership
    - Show cluster summary overlays
    - Enable cluster-based filtering
    """

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    clusters: list[dict[str, Any]]
    cluster_edges: list[dict[str, Any]]  # Aggregated edges between clusters
    total_nodes: int  # REAL total in graph (not limited)
    total_edges: int  # REAL total in graph (not limited)
    displayed_nodes: int  # How many we're sending to UI
    displayed_edges: int  # How many we're sending to UI


async def _get_graph_totals(
    client: GraphClient,
    organization_id: str,
    project_ids: list[str] | None = None,
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
    total_nodes = 0
    total_edges = 0

    # Handle special __unassigned__ filter for entities without a project
    UNASSIGNED_ID = "__unassigned__"
    has_unassigned = project_ids and UNASSIGNED_ID in project_ids
    real_project_ids = [pid for pid in (project_ids or []) if pid != UNASSIGNED_ID]

    # For specific projects with neighbors, use UNION query
    # For unassigned or no filter, use simple query
    use_transitive = include_neighbors and real_project_ids and not has_unassigned

    params: dict[str, str | list[str]] = {"group_id": organization_id}
    if real_project_ids:
        params["project_ids"] = real_project_ids

    try:
        if use_transitive:
            # Include nodes with direct project_id + their 1-hop neighbors
            node_query = """
            MATCH (n)
            WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
              AND (n.project_id IN $project_ids OR n.uuid IN $project_ids)
            RETURN count(DISTINCT n) AS cnt
            UNION ALL
            MATCH (n)-[]-(neighbor)
            WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
              AND (neighbor.project_id IN $project_ids OR neighbor.uuid IN $project_ids)
            RETURN count(DISTINCT n) AS cnt
            """
        elif has_unassigned and real_project_ids:
            # Both unassigned AND specific projects
            node_query = """
            MATCH (n)
            WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
              AND (n.project_id IS NULL OR n.uuid IN $project_ids OR n.project_id IN $project_ids)
            RETURN count(n) AS cnt
            """
        elif has_unassigned:
            # Only unassigned
            node_query = """
            MATCH (n)
            WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
              AND n.project_id IS NULL
            RETURN count(n) AS cnt
            """
        elif real_project_ids:
            # Specific projects without neighbors (fallback)
            node_query = """
            MATCH (n)
            WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
              AND (n.uuid IN $project_ids OR n.project_id IN $project_ids)
            RETURN count(n) AS cnt
            """
        else:
            # No filter - count all
            node_query = """
            MATCH (n)
            WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
            RETURN count(n) AS cnt
            """

        result = await client.execute_read_org(node_query, organization_id, **params)
        if result:
            # For UNION ALL, we get multiple rows - sum them
            for record in result:
                cnt = record[0] if isinstance(record, (list, tuple)) else record.get("cnt", 0)
                total_nodes += cnt
    except Exception as e:
        log.warning("count_nodes_failed", error=str(e))

    # Edge count - for transitive, count edges between matching nodes
    try:
        if use_transitive:
            # For transitive, count edges where at least one endpoint is a seed node
            # This is simpler and still gives a useful count
            edge_query = """
            MATCH (a)-[r]->(b)
            WHERE r.group_id = $group_id
              AND (a:Episodic OR a:Entity) AND a.group_id = $group_id
              AND (b:Episodic OR b:Entity) AND b.group_id = $group_id
              AND (
                a.project_id IN $project_ids OR a.uuid IN $project_ids
                OR b.project_id IN $project_ids OR b.uuid IN $project_ids
              )
            RETURN count(r) AS cnt
            """
        elif project_ids:
            # Non-transitive project filter
            if has_unassigned and real_project_ids:
                node_filter = "(n.project_id IS NULL OR n.uuid IN $project_ids OR n.project_id IN $project_ids)"
            elif has_unassigned:
                node_filter = "n.project_id IS NULL"
            else:
                node_filter = "(n.uuid IN $project_ids OR n.project_id IN $project_ids)"

            a_filter = node_filter.replace("n.", "a.")
            b_filter = node_filter.replace("n.", "b.")
            edge_query = f"""
            MATCH (a)-[r]->(b)
            WHERE (a:Episodic OR a:Entity) AND a.group_id = $group_id AND {a_filter}
              AND (b:Episodic OR b:Entity) AND b.group_id = $group_id AND {b_filter}
              AND r.group_id = $group_id
            RETURN count(r) AS cnt
            """
        else:
            edge_query = "MATCH ()-[r]->() WHERE r.group_id = $group_id RETURN count(r) AS cnt"

        result = await client.execute_read_org(edge_query, organization_id, **params)
        if result:
            record = result[0]
            total_edges = record[0] if isinstance(record, (list, tuple)) else record.get("cnt", 0)
    except Exception as e:
        log.warning("count_edges_failed", error=str(e))

    return total_nodes, total_edges


def _extract_entity_type(
    entity_type: str | None, labels: list[str] | None, name: str | None = None
) -> str:
    """Extract entity type from entity_type property, labels array, or infer from name.

    Graphiti stores entity types in two places:
    - n.entity_type: Direct property (preferred)
    - n.labels: Array like [Entity, pattern] where second element may be the type

    If neither has type info, try to infer from the entity name.
    """
    if entity_type:
        return entity_type

    # Try to extract from labels array - skip known graph/system labels
    if labels:
        skip_labels = {
            "Entity",
            "Episodic",
            "EntityNode",
            "EpisodicNode",
            "Cluster",
            "Community",
            "Node",  # System labels
        }
        for label in labels:
            if label and label not in skip_labels:
                return label.lower()

    # Infer type from name patterns
    if name:
        name_lower = name.lower()
        # File paths
        if any(
            name_lower.endswith(ext)
            for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".rs", ".go", ".md")
        ):
            return "file"
        # URLs
        if name_lower.startswith(("http://", "https://", "www.")):
            return "source"
        # Code-like names (functions, classes)
        if "(" in name or name.endswith("()"):
            return "function"

    # Default to "topic" - most extracted entities without explicit types are topics
    return "topic"


async def _fetch_graph_nodes(
    client: GraphClient,
    organization_id: str,
    node_to_cluster: dict[str, str],
    max_nodes: int,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
) -> tuple[list[dict[str, Any]], set[str]]:
    """Fetch nodes with cluster assignments, optionally filtered by project/type.

    When filtering by project, includes:
    - Nodes with direct project_id matching the filter
    - 1-hop neighbors (e.g., episodes related to tasks in the project)
    """
    # Handle special __unassigned__ filter for entities without a project
    UNASSIGNED_ID = "__unassigned__"
    has_unassigned = project_ids and UNASSIGNED_ID in project_ids
    real_project_ids = [pid for pid in (project_ids or []) if pid != UNASSIGNED_ID]

    # Decide whether to use transitive (1-hop neighbor) filtering
    use_transitive = real_project_ids and not has_unassigned

    # Build entity type filter if provided
    type_filter = ""
    if entity_types:
        type_list = ", ".join(f"'{t}'" for t in entity_types)
        type_filter = f" AND n.entity_type IN [{type_list}]"

    params: dict[str, Any] = {"group_id": organization_id, "limit": max_nodes}
    if real_project_ids:
        params["project_ids"] = real_project_ids

    if use_transitive:
        # Use UNION to get direct project members + 1-hop neighbors
        query = f"""
        MATCH (n)
        WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
          AND (n.project_id IN $project_ids OR n.uuid IN $project_ids){type_filter}
        RETURN DISTINCT n.uuid AS id, n.name AS name, n.entity_type AS type,
               n.summary AS summary, n.labels AS labels, n.project_id AS project_id
        UNION
        MATCH (n)-[]-(neighbor)
        WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
          AND (neighbor.project_id IN $project_ids OR neighbor.uuid IN $project_ids){type_filter}
        RETURN DISTINCT n.uuid AS id, n.name AS name, n.entity_type AS type,
               n.summary AS summary, n.labels AS labels, n.project_id AS project_id
        LIMIT $limit
        """
    else:
        # Build simple filter for unassigned or no-project case
        filters = ["(n:Episodic OR n:Entity)", "n.group_id = $group_id"]

        if has_unassigned and real_project_ids:
            filters.append(
                "(n.project_id IS NULL OR n.uuid IN $project_ids OR n.project_id IN $project_ids)"
            )
        elif has_unassigned:
            filters.append("n.project_id IS NULL")
        elif real_project_ids:
            filters.append("(n.uuid IN $project_ids OR n.project_id IN $project_ids)")

        if entity_types:
            type_list = ", ".join(f"'{t}'" for t in entity_types)
            filters.append(f"n.entity_type IN [{type_list}]")

        where_clause = " AND ".join(filters)
        query = f"""
        MATCH (n)
        WHERE {where_clause}
        RETURN n.uuid AS id, n.name AS name, n.entity_type AS type,
               n.summary AS summary, n.labels AS labels, n.project_id AS project_id
        LIMIT $limit
        """

    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    try:
        result = await client.execute_read_org(query, organization_id, **params)
        for record in result:
            if isinstance(record, (list, tuple)):
                node_id = record[0] if len(record) > 0 else None
                name = record[1] if len(record) > 1 else ""
                entity_type = record[2] if len(record) > 2 else None
                summary = record[3] if len(record) > 3 else ""
                labels = record[4] if len(record) > 4 else None
            else:
                node_id = record.get("id")
                name = record.get("name", "")
                entity_type = record.get("type")
                summary = record.get("summary", "")
                labels = record.get("labels")

            if node_id:
                resolved_type = _extract_entity_type(entity_type, labels, name)
                node_ids.add(node_id)
                nodes.append(
                    {
                        "id": node_id,
                        "name": name or node_id[:20],
                        "type": resolved_type,
                        "summary": summary or "",
                        "cluster_id": node_to_cluster.get(node_id, "unclustered"),
                    }
                )
    except Exception as e:
        log.warning("fetch_nodes_failed", error=str(e))

    return nodes, node_ids


async def _fetch_graph_edges(
    client: GraphClient,
    organization_id: str,
    node_ids: set[str],
    max_edges: int,
) -> list[dict[str, Any]]:
    """Fetch edges between nodes in our set."""
    query = """
    MATCH (a)-[r]->(b)
    WHERE r.group_id = $group_id
    RETURN a.uuid AS source, b.uuid AS target, COALESCE(r.name, type(r)) AS type
    LIMIT $limit
    """
    edges: list[dict[str, Any]] = []

    try:
        result = await client.execute_read_org(
            query, organization_id, group_id=organization_id, limit=max_edges
        )
        for record in result:
            if isinstance(record, (list, tuple)):
                source, target, rel_type = (
                    record[0] if len(record) > 0 else None,
                    record[1] if len(record) > 1 else None,
                    record[2] if len(record) > 2 else "RELATED",
                )
            else:
                source = record.get("source")
                target = record.get("target")
                rel_type = record.get("type", "RELATED")

            if source and target and source in node_ids and target in node_ids:
                edges.append({"source": source, "target": target, "type": rel_type or "RELATED"})
    except Exception as e:
        log.warning("fetch_edges_failed", error=str(e))

    return edges


def _build_cluster_metadata(
    nodes: list[dict[str, Any]],
    clusters_meta: list[dict[str, Any]],
    node_to_cluster: dict[str, str],
    edges: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build enriched cluster metadata and inter-cluster edges."""
    # Count entity types per cluster
    cluster_type_counts: dict[str, dict[str, int]] = {}
    for node in nodes:
        cluster_id = node["cluster_id"]
        entity_type = node["type"]
        if cluster_id not in cluster_type_counts:
            cluster_type_counts[cluster_id] = {}
        cluster_type_counts[cluster_id][entity_type] = (
            cluster_type_counts[cluster_id].get(entity_type, 0) + 1
        )

    # Enrich clusters with type distribution (only include clusters with displayed nodes)
    enriched_clusters = []
    for cluster in clusters_meta:
        type_dist = cluster_type_counts.get(cluster["id"], {})
        if not type_dist:
            # Skip clusters with no nodes in the current filtered view
            continue
        displayed_count = sum(type_dist.values())
        dominant = max(type_dist.items(), key=lambda x: x[1])[0]
        enriched_clusters.append(
            {
                **cluster,
                "type_distribution": type_dist,
                "dominant_type": dominant,
                "member_count": displayed_count,  # Override with actual displayed count
            }
        )

    # Add unclustered pseudo-cluster if needed
    unclustered_types = cluster_type_counts.get("unclustered", {})
    if unclustered_types:
        dominant = (
            max(unclustered_types.items(), key=lambda x: x[1])[0]
            if unclustered_types
            else "unknown"
        )
        enriched_clusters.append(
            {
                "id": "unclustered",
                "member_count": sum(unclustered_types.values()),
                "level": 0,
                "type_distribution": unclustered_types,
                "dominant_type": dominant,
            }
        )

    # Calculate inter-cluster edges
    cluster_edge_counts: dict[tuple[str, str], int] = {}
    for edge in edges:
        src_cluster = node_to_cluster.get(edge["source"], "unclustered")
        tgt_cluster = node_to_cluster.get(edge["target"], "unclustered")
        if src_cluster != tgt_cluster:
            sorted_pair = sorted([src_cluster, tgt_cluster])
            pair: tuple[str, str] = (sorted_pair[0], sorted_pair[1])
            cluster_edge_counts[pair] = cluster_edge_counts.get(pair, 0) + 1

    cluster_edges = [
        {"source": p[0], "target": p[1], "weight": c}
        for p, c in cluster_edge_counts.items()
        if c > 0
    ]

    return enriched_clusters, cluster_edges


async def get_hierarchical_graph(
    client: GraphClient,
    organization_id: str,
    project_ids: list[str] | None = None,
    entity_types: list[str] | None = None,
    max_nodes: int = 1000,
    max_edges: int = 5000,
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

    Returns:
        HierarchicalGraphData with nodes, edges, and cluster metadata.
    """
    log.info(
        "get_hierarchical_graph_start",
        org_id=organization_id,
        max_nodes=max_nodes,
        projects=project_ids,
        types=entity_types,
    )

    # Get real totals for stats display (filtered by project if specified)
    total_node_count, total_edge_count = await _get_graph_totals(
        client, organization_id, project_ids=project_ids
    )
    log.info(
        "graph_totals_queried",
        total_nodes=total_node_count,
        total_edges=total_edge_count,
        filtered_by_projects=bool(project_ids),
    )

    # Check cache for community detection (expensive operation)
    # Cache key includes org only - community structure is org-wide
    cache_key = organization_id
    node_to_cluster: dict[str, str] = {}
    clusters_meta: list[dict[str, Any]] = []

    if cache_key in HIERARCHICAL_CACHE:
        cached_at, cached_clusters, cached_meta = HIERARCHICAL_CACHE[cache_key]
        if datetime.now(UTC) - cached_at < HIERARCHICAL_CACHE_TTL:
            log.info("hierarchical_cache_hit", org_id=organization_id)
            node_to_cluster = cached_clusters
            clusters_meta = cached_meta
        else:
            log.debug("hierarchical_cache_expired", org_id=organization_id)

    # Run community detection if not cached
    if not node_to_cluster:
        try:
            detected = await detect_communities(
                client,
                organization_id,
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
                HIERARCHICAL_CACHE[cache_key] = (datetime.now(UTC), node_to_cluster, clusters_meta)
            else:
                log.warning("community_detection_empty", msg="no communities detected")
        except ImportError:
            log.warning("networkx_not_available", msg="community detection unavailable")
        except Exception as e:
            log.warning("community_detection_failed", error=str(e))

    # Fetch nodes and edges with optional project/type filtering
    nodes, node_ids = await _fetch_graph_nodes(
        client,
        organization_id,
        node_to_cluster,
        max_nodes,
        project_ids=project_ids,
        entity_types=entity_types,
    )
    edges = await _fetch_graph_edges(client, organization_id, node_ids, max_edges)

    # Build cluster metadata
    enriched_clusters, cluster_edges = _build_cluster_metadata(
        nodes, clusters_meta, node_to_cluster, edges
    )

    log.info(
        "get_hierarchical_graph_complete",
        total_nodes=total_node_count,
        total_edges=total_edge_count,
        displayed_nodes=len(nodes),
        displayed_edges=len(edges),
        clusters=len(enriched_clusters),
    )

    return HierarchicalGraphData(
        nodes=nodes,
        edges=edges,
        clusters=enriched_clusters,
        cluster_edges=cluster_edges,
        total_nodes=total_node_count,
        total_edges=total_edge_count,
        displayed_nodes=len(nodes),
        displayed_edges=len(edges),
    )


@dataclass
class CommunityConfig:
    """Configuration for community detection.

    Attributes:
        resolutions: Resolution parameters for hierarchical levels.
                    Higher resolution = more smaller communities.
        min_community_size: Minimum members to form a community.
        max_levels: Maximum hierarchy levels to compute.
        store_in_graph: Whether to persist communities to graph.
    """

    resolutions: list[float] = field(default_factory=lambda: [0.5, 1.0, 2.0])
    min_community_size: int = 2
    max_levels: int = 3
    store_in_graph: bool = True


@dataclass
class DetectedCommunity:
    """A detected community before being stored.

    Attributes:
        id: Unique community identifier.
        member_ids: Entity IDs in this community.
        level: Hierarchy level (0 = leaf, higher = broader).
        resolution: Resolution parameter used for detection.
        modularity: Modularity score.
    """

    id: str
    member_ids: list[str]
    level: int
    resolution: float
    modularity: float = 0.0
    parent_id: str | None = None
    child_ids: list[str] = field(default_factory=list)

    @property
    def member_count(self) -> int:
        """Number of members in this community."""
        return len(self.member_ids)


async def export_to_networkx(
    client: GraphClient,
    organization_id: str,
    type_affinity_weight: float = 2.0,
) -> Any:
    """Export knowledge graph to NetworkX format with type affinity.

    Edges between nodes of the same entity type get higher weight,
    encouraging the Louvain algorithm to cluster same-type nodes together.

    Args:
        client: Graph client.
        organization_id: Organization UUID for filtering.
        type_affinity_weight: Extra weight for same-type connections (default 2.0).

    Returns:
        NetworkX graph object.

    Raises:
        ImportError: If networkx is not installed.
    """
    try:
        import networkx as nx  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "networkx is required for community detection. Install with: pip install networkx"
        ) from e

    log.info("export_to_networkx_start", org_id=organization_id, type_affinity=type_affinity_weight)

    # Create undirected graph for community detection
    G = nx.Graph()

    # Fetch ALL nodes - both Episodic and Entity labels with group_id filter
    # Also fetch labels array for type resolution
    node_query = """
    MATCH (n)
    WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
    RETURN n.uuid AS id, n.name AS name, n.entity_type AS type, n.labels AS labels
    """

    try:
        node_result = await client.execute_read_org(
            node_query, organization_id, group_id=organization_id
        )

        for record in node_result:
            if isinstance(record, (list, tuple)):
                node_id = record[0] if len(record) > 0 else None
                name = record[1] if len(record) > 1 else ""
                entity_type = record[2] if len(record) > 2 else None
                labels = record[3] if len(record) > 3 else None
            else:
                node_id = record.get("id")
                name = record.get("name", "")
                entity_type = record.get("type")
                labels = record.get("labels")

            if node_id:
                # Resolve entity type using helper
                resolved_type = _extract_entity_type(entity_type, labels, name)
                G.add_node(node_id, name=name, type=resolved_type)

    except Exception as e:
        log.warning("export_nodes_failed", error=str(e))

    # Fetch ALL edges - use group_id filter on relationship
    edge_query = """
    MATCH (a)-[r]->(b)
    WHERE r.group_id = $group_id
    RETURN a.uuid AS source, b.uuid AS target, type(r) AS rel_type
    """

    try:
        edge_result = await client.execute_read_org(
            edge_query, organization_id, group_id=organization_id
        )

        for record in edge_result:
            if isinstance(record, (list, tuple)):
                source = record[0] if len(record) > 0 else None
                target = record[1] if len(record) > 1 else None
                rel_type = record[2] if len(record) > 2 else ""
            else:
                source = record.get("source")
                target = record.get("target")
                rel_type = record.get("rel_type", "")

            if source and target and source in G and target in G:
                # Calculate edge weight with type affinity boost
                source_type = G.nodes[source].get("type", "")
                target_type = G.nodes[target].get("type", "")

                # Base weight + bonus if same type
                weight = 1.0
                if source_type and target_type and source_type == target_type:
                    weight += type_affinity_weight

                # Update or add edge (accumulate weight for multi-edges)
                if G.has_edge(source, target):
                    G[source][target]["weight"] += weight
                else:
                    G.add_edge(source, target, rel_type=rel_type, weight=weight)

    except Exception as e:
        log.warning("export_edges_failed", error=str(e))

    log.info(
        "export_to_networkx_complete",
        org_id=organization_id,
        nodes=G.number_of_nodes(),
        edges=G.number_of_edges(),
    )

    return G


def detect_communities_louvain(
    G: Any,
    resolution: float = 1.0,
) -> tuple[dict[str, int], float]:
    """Detect communities using Louvain algorithm.

    Args:
        G: NetworkX graph.
        resolution: Resolution parameter (higher = more communities).

    Returns:
        Tuple of (node_id -> community_id mapping, modularity score).

    Raises:
        ImportError: If python-louvain is not installed.
    """
    try:
        import community as community_louvain
    except ImportError as e:
        raise ImportError(
            "python-louvain is required for community detection. "
            "Install with: pip install python-louvain"
        ) from e

    if G.number_of_nodes() == 0:
        return {}, 0.0

    # Run Louvain algorithm
    partition = community_louvain.best_partition(G, resolution=resolution)
    modularity = community_louvain.modularity(partition, G)

    return partition, modularity


def detect_communities_leiden(
    G: Any,
    resolution: float = 1.0,
) -> tuple[dict[str, int], float]:
    """Detect communities using Leiden algorithm.

    Args:
        G: NetworkX graph.
        resolution: Resolution parameter (higher = more communities).

    Returns:
        Tuple of (node_id -> community_id mapping, modularity score).

    Raises:
        ImportError: If leidenalg/igraph is not installed.
    """
    try:
        import igraph as ig  # type: ignore[import-not-found]
        import leidenalg  # type: ignore[import-not-found]
    except ImportError as e:
        raise ImportError(
            "leidenalg and igraph are required for Leiden algorithm. "
            "Install with: pip install leidenalg igraph"
        ) from e

    if G.number_of_nodes() == 0:
        return {}, 0.0

    # Convert NetworkX to igraph
    G_ig = ig.Graph.from_networkx(G)

    # Run Leiden algorithm
    partition = leidenalg.find_partition(
        G_ig,
        leidenalg.CPMVertexPartition,
        resolution_parameter=resolution,
    )

    # Map back to node IDs
    node_ids = list(G.nodes())
    partition_dict = {node_ids[i]: partition.membership[i] for i in range(len(node_ids))}

    # Calculate modularity
    modularity = partition.quality() / (2 * G.number_of_edges()) if G.number_of_edges() > 0 else 0.0

    return partition_dict, modularity


def partition_to_communities(
    partition: dict[str, int],
    level: int,
    resolution: float,
    modularity: float,
    min_size: int = 2,
) -> list[DetectedCommunity]:
    """Convert partition dict to list of communities.

    Args:
        partition: Node ID -> community number mapping.
        level: Hierarchy level.
        resolution: Resolution used for detection.
        modularity: Overall modularity score.
        min_size: Minimum community size.

    Returns:
        List of DetectedCommunity objects.
    """
    # Group nodes by community
    community_members: dict[int, list[str]] = {}
    for node_id, comm_id in partition.items():
        if comm_id not in community_members:
            community_members[comm_id] = []
        community_members[comm_id].append(node_id)

    # Create community objects
    communities: list[DetectedCommunity] = []
    for comm_num, members in community_members.items():
        if len(members) < min_size:
            continue

        community = DetectedCommunity(
            id=f"comm_L{level}_{comm_num}_{uuid.uuid4().hex[:8]}",
            member_ids=sorted(members),
            level=level,
            resolution=resolution,
            modularity=modularity,
        )
        communities.append(community)

    return communities


def link_hierarchy(
    all_communities: list[list[DetectedCommunity]],
) -> list[DetectedCommunity]:
    """Link communities across hierarchy levels.

    Lower-level communities that are subsets of higher-level
    communities become children.

    Args:
        all_communities: List of community lists by level.

    Returns:
        Flattened list with parent/child links set.
    """
    if not all_communities:
        return []

    flat: list[DetectedCommunity] = []

    for level_idx, level_communities in enumerate(all_communities):
        for community in level_communities:
            # Find parent at next level
            if level_idx < len(all_communities) - 1:
                parent_level = all_communities[level_idx + 1]
                member_set = set(community.member_ids)

                for parent in parent_level:
                    parent_set = set(parent.member_ids)
                    # Check if this community is a subset of parent
                    if member_set <= parent_set:
                        community.parent_id = parent.id
                        parent.child_ids.append(community.id)
                        break

            flat.append(community)

    return flat


async def detect_communities(
    client: GraphClient,
    organization_id: str,
    config: CommunityConfig | None = None,
    algorithm: str = "louvain",
) -> list[DetectedCommunity]:
    """Detect hierarchical communities in the knowledge graph.

    Args:
        client: Graph client.
        config: Detection configuration.
        algorithm: "louvain" or "leiden".

    Returns:
        List of detected communities with hierarchy links.
    """
    if config is None:
        config = CommunityConfig()

    log.info(
        "detect_communities_start",
        algorithm=algorithm,
        resolutions=config.resolutions,
        max_levels=config.max_levels,
    )

    # Export graph to NetworkX
    G = await export_to_networkx(client, organization_id)

    if G.number_of_nodes() < config.min_community_size:
        log.info("detect_communities_too_few_nodes", nodes=G.number_of_nodes())
        return []

    # Select algorithm
    detect_fn = detect_communities_leiden if algorithm == "leiden" else detect_communities_louvain

    # Detect communities at each resolution level
    all_level_communities: list[list[DetectedCommunity]] = []

    for level, resolution in enumerate(config.resolutions[: config.max_levels]):
        try:
            partition, modularity = detect_fn(G, resolution=resolution)

            communities = partition_to_communities(
                partition=partition,
                level=level,
                resolution=resolution,
                modularity=modularity,
                min_size=config.min_community_size,
            )

            all_level_communities.append(communities)

            log.debug(
                "detect_communities_level_complete",
                level=level,
                resolution=resolution,
                communities=len(communities),
                modularity=modularity,
            )

        except ImportError as e:
            log.exception("detect_communities_missing_dependency", error=str(e))
            raise
        except Exception as e:
            log.warning("detect_communities_level_failed", level=level, error=str(e))
            continue

    # Link hierarchy
    all_communities = link_hierarchy(all_level_communities)

    log.info(
        "detect_communities_complete",
        total_communities=len(all_communities),
        levels=len(all_level_communities),
    )

    return all_communities


async def store_communities(
    client: GraphClient,
    organization_id: str,
    communities: list[DetectedCommunity],
    clear_existing: bool = True,
) -> int:
    """Store detected communities in the graph.

    Args:
        client: Graph client.
        communities: Communities to store.
        clear_existing: Whether to clear existing communities first.

    Returns:
        Number of communities stored.
    """
    if not communities:
        return 0

    log.info("store_communities_start", count=len(communities), clear_existing=clear_existing)

    # Clear existing communities if requested
    if clear_existing:
        clear_query = """
        MATCH (c:Entity {entity_type: 'community'})
        DETACH DELETE c
        """
        try:
            await client.execute_write_org(clear_query, organization_id)
        except Exception as e:
            log.warning("clear_communities_failed", error=str(e))

    # Store each community
    stored = 0
    now = datetime.now(UTC).isoformat()

    for community in communities:
        create_query = """
        CREATE (c:Entity {
            uuid: $id,
            entity_type: 'community',
            name: $name,
            member_ids: $member_ids,
            member_count: $member_count,
            level: $level,
            resolution: $resolution,
            modularity: $modularity,
            parent_community_id: $parent_id,
            child_community_ids: $child_ids,
            created_at: $created_at
        })
        RETURN c.uuid AS id
        """

        # Generate name
        name = f"Community L{community.level} ({community.member_count} members)"

        try:
            await client.execute_write_org(
                create_query,
                organization_id,
                id=community.id,
                name=name,
                member_ids=community.member_ids,
                member_count=community.member_count,
                level=community.level,
                resolution=community.resolution,
                modularity=community.modularity,
                parent_id=community.parent_id,
                child_ids=community.child_ids,
                created_at=now,
            )
            stored += 1
        except Exception as e:
            log.warning("store_community_failed", community_id=community.id, error=str(e))

    # Create BELONGS_TO relationships from members to communities
    for community in communities:
        for member_id in community.member_ids:
            link_query = """
            MATCH (e:Entity {uuid: $entity_id}), (c:Entity {uuid: $community_id})
            MERGE (e)-[:BELONGS_TO]->(c)
            """
            with contextlib.suppress(Exception):
                await client.execute_write_org(
                    link_query,
                    organization_id,
                    entity_id=member_id,
                    community_id=community.id,
                )

    log.info("store_communities_complete", stored=stored)
    return stored


async def get_entity_communities(
    client: GraphClient,
    organization_id: str,
    entity_id: str,
) -> list[dict[str, Any]]:
    """Get communities that an entity belongs to.

    Args:
        client: Graph client.
        entity_id: Entity UUID.

    Returns:
        List of community info dicts.
    """
    query = """
    MATCH (e:Entity {uuid: $entity_id})-[:BELONGS_TO]->(c:Entity {entity_type: 'community'})
    RETURN c.uuid AS id,
           c.name AS name,
           c.level AS level,
           c.member_count AS member_count,
           c.summary AS summary
    ORDER BY c.level
    """

    communities: list[dict[str, Any]] = []

    try:
        result = await client.execute_read_org(query, organization_id, entity_id=entity_id)

        for record in result:
            if isinstance(record, (list, tuple)):
                comm = {
                    "id": record[0] if len(record) > 0 else None,
                    "name": record[1] if len(record) > 1 else "",
                    "level": record[2] if len(record) > 2 else 0,
                    "member_count": record[3] if len(record) > 3 else 0,
                    "summary": record[4] if len(record) > 4 else "",
                }
            else:
                comm = {
                    "id": record.get("id"),
                    "name": record.get("name", ""),
                    "level": record.get("level", 0),
                    "member_count": record.get("member_count", 0),
                    "summary": record.get("summary", ""),
                }
            if comm["id"]:
                communities.append(comm)

    except Exception as e:
        log.warning("get_entity_communities_failed", entity_id=entity_id, error=str(e))

    return communities


async def get_community_members(
    client: GraphClient,
    organization_id: str,
    community_id: str,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Get members of a community.

    Args:
        client: Graph client.
        community_id: Community UUID.
        limit: Maximum members to return.

    Returns:
        List of member entity info.
    """
    query = """
    MATCH (c:Entity {uuid: $community_id})<-[:BELONGS_TO]-(e:Entity)
    RETURN e.uuid AS id,
           e.name AS name,
           e.entity_type AS type,
           e.description AS description
    LIMIT $limit
    """

    members: list[dict[str, Any]] = []

    try:
        result = await client.execute_read_org(
            query,
            organization_id,
            community_id=community_id,
            limit=limit,
        )

        for record in result:
            if isinstance(record, (list, tuple)):
                member = {
                    "id": record[0] if len(record) > 0 else None,
                    "name": record[1] if len(record) > 1 else "",
                    "type": record[2] if len(record) > 2 else "",
                    "description": record[3] if len(record) > 3 else "",
                }
            else:
                member = {
                    "id": record.get("id"),
                    "name": record.get("name", ""),
                    "type": record.get("type", ""),
                    "description": record.get("description", ""),
                }
            if member["id"]:
                members.append(member)

    except Exception as e:
        log.warning("get_community_members_failed", community_id=community_id, error=str(e))

    return members
