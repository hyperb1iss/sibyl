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
    from sibyl.graph.client import GraphClient

log = structlog.get_logger()

# =============================================================================
# Cluster Cache for Visualization
# =============================================================================

CLUSTER_CACHE: dict[str, tuple[datetime, list["ClusterSummary"]]] = {}
CLUSTER_CACHE_TTL = timedelta(minutes=5)


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
    client: "GraphClient",
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
    client: "GraphClient",
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
    client: "GraphClient",
    organization_id: str,
    detected: list["DetectedCommunity"],
) -> list[ClusterSummary]:
    """Convert DetectedCommunity to ClusterSummary with type distribution."""
    summaries = []

    for community in detected:
        if not community.member_ids:
            continue

        # Query type distribution for this cluster's members
        query = """
        MATCH (n)
        WHERE n.uuid IN $ids
        RETURN n.entity_type AS type, count(*) AS cnt
        """

        type_dist: dict[str, int] = {}
        try:
            result = await client.execute_read_org(
                query, organization_id, ids=community.member_ids
            )
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
    client: "GraphClient",
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
                nodes.append({
                    "id": record[0],
                    "name": record[1] or record[0][:20],
                    "type": record[2] or "unknown",
                    "summary": record[3] or "",
                })
            else:
                nodes.append({
                    "id": record.get("id"),
                    "name": record.get("name") or record.get("id", "")[:20],
                    "type": record.get("type", "unknown"),
                    "summary": record.get("summary", ""),
                })
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
                edges.append({
                    "source": record[0],
                    "target": record[1],
                    "type": record[2] or "RELATED",
                })
            else:
                edges.append({
                    "source": record.get("source"),
                    "target": record.get("target"),
                    "type": record.get("rel_type", "RELATED"),
                })
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


async def get_hierarchical_graph(
    client: "GraphClient",
    organization_id: str,
    max_nodes: int = 1000,
    max_edges: int = 5000,
    force_refresh: bool = False,
) -> HierarchicalGraphData:
    """Get graph data with cluster assignments for rich visualization.

    Returns actual nodes and edges (not aggregated bubbles) with each node
    assigned to a cluster based on Louvain community detection.

    Args:
        client: Graph client.
        organization_id: Organization UUID.
        max_nodes: Maximum nodes to return (will sample if exceeded).
        max_edges: Maximum edges to return.
        force_refresh: Bypass cluster cache.

    Returns:
        HierarchicalGraphData with nodes, edges, and cluster metadata.
    """
    log.info(
        "get_hierarchical_graph_start",
        org_id=organization_id,
        max_nodes=max_nodes,
        max_edges=max_edges,
    )

    # First, get REAL total counts (no LIMIT) for stats display
    total_node_count = 0
    total_edge_count = 0

    count_node_query = """
    MATCH (n)
    WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
    RETURN count(n) AS cnt
    """

    count_edge_query = """
    MATCH ()-[r]->()
    WHERE r.group_id = $group_id
    RETURN count(r) AS cnt
    """

    try:
        node_count_result = await client.execute_read_org(
            count_node_query, organization_id, group_id=organization_id
        )
        if node_count_result:
            record = node_count_result[0]
            total_node_count = record[0] if isinstance(record, (list, tuple)) else record.get("cnt", 0)
    except Exception as e:
        log.warning("count_nodes_failed", error=str(e))

    try:
        edge_count_result = await client.execute_read_org(
            count_edge_query, organization_id, group_id=organization_id
        )
        if edge_count_result:
            record = edge_count_result[0]
            total_edge_count = record[0] if isinstance(record, (list, tuple)) else record.get("cnt", 0)
    except Exception as e:
        log.warning("count_edges_failed", error=str(e))

    log.info(
        "graph_totals_queried",
        total_nodes=total_node_count,
        total_edges=total_edge_count,
    )

    # Get or compute cluster assignments
    node_to_cluster: dict[str, str] = {}
    clusters_meta: list[dict[str, Any]] = []

    try:
        # Try to get real community detection
        detected = await detect_communities(
            client,
            organization_id,
            config=CommunityConfig(
                resolutions=[1.0],
                min_community_size=2,
                max_levels=1,
                store_in_graph=False,
            ),
            algorithm="louvain",
        )

        if detected:
            # Build node -> cluster mapping
            for community in detected:
                cluster_id = community.id
                for member_id in community.member_ids:
                    node_to_cluster[member_id] = cluster_id

            # Build cluster metadata
            clusters_meta = [
                {
                    "id": c.id,
                    "member_count": c.member_count,
                    "level": c.level,
                }
                for c in detected
            ]
            log.info(
                "community_detection_success",
                clusters=len(detected),
                assigned_nodes=len(node_to_cluster),
            )
        else:
            log.warning("community_detection_empty", msg="no communities detected")

    except ImportError:
        log.warning("networkx_not_available", msg="community detection unavailable")
    except Exception as e:
        log.warning("community_detection_failed", error=str(e))

    # Fetch nodes with limit
    node_query = """
    MATCH (n)
    WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
    RETURN n.uuid AS id,
           n.name AS name,
           n.entity_type AS type,
           n.summary AS summary
    LIMIT $limit
    """

    nodes: list[dict[str, Any]] = []
    node_ids: set[str] = set()

    try:
        result = await client.execute_read_org(
            node_query, organization_id, group_id=organization_id, limit=max_nodes
        )

        for record in result:
            if isinstance(record, (list, tuple)):
                node_id = record[0] if len(record) > 0 else None
                name = record[1] if len(record) > 1 else ""
                entity_type = record[2] if len(record) > 2 else "unknown"
                summary = record[3] if len(record) > 3 else ""
            else:
                node_id = record.get("id")
                name = record.get("name", "")
                entity_type = record.get("type", "unknown")
                summary = record.get("summary", "")

            if node_id:
                node_ids.add(node_id)
                # Assign cluster - use "unclustered" if not in a community
                cluster_id = node_to_cluster.get(node_id, "unclustered")

                nodes.append({
                    "id": node_id,
                    "name": name or node_id[:20],
                    "type": entity_type or "unknown",
                    "summary": summary or "",
                    "cluster_id": cluster_id,
                })

    except Exception as e:
        log.warning("fetch_nodes_failed", error=str(e))

    # Fetch edges between the nodes we have
    edge_query = """
    MATCH (a)-[r]->(b)
    WHERE r.group_id = $group_id
    RETURN a.uuid AS source,
           b.uuid AS target,
           COALESCE(r.name, type(r)) AS type
    LIMIT $limit
    """

    edges: list[dict[str, Any]] = []

    try:
        result = await client.execute_read_org(
            edge_query, organization_id, group_id=organization_id, limit=max_edges
        )

        for record in result:
            if isinstance(record, (list, tuple)):
                source = record[0] if len(record) > 0 else None
                target = record[1] if len(record) > 1 else None
                rel_type = record[2] if len(record) > 2 else "RELATED"
            else:
                source = record.get("source")
                target = record.get("target")
                rel_type = record.get("type", "RELATED")

            # Only include edges where both endpoints are in our node set
            if source and target and source in node_ids and target in node_ids:
                edges.append({
                    "source": source,
                    "target": target,
                    "type": rel_type or "RELATED",
                })

    except Exception as e:
        log.warning("fetch_edges_failed", error=str(e))

    # Build cluster type distribution and colors
    cluster_type_counts: dict[str, dict[str, int]] = {}
    for node in nodes:
        cluster_id = node["cluster_id"]
        entity_type = node["type"]
        if cluster_id not in cluster_type_counts:
            cluster_type_counts[cluster_id] = {}
        cluster_type_counts[cluster_id][entity_type] = (
            cluster_type_counts[cluster_id].get(entity_type, 0) + 1
        )

    # Enrich clusters_meta with type distribution
    enriched_clusters = []
    for cluster in clusters_meta:
        cluster_id = cluster["id"]
        type_dist = cluster_type_counts.get(cluster_id, {})
        dominant_type = max(type_dist.items(), key=lambda x: x[1])[0] if type_dist else "unknown"
        enriched_clusters.append({
            **cluster,
            "type_distribution": type_dist,
            "dominant_type": dominant_type,
        })

    # Add unclustered "cluster" if any nodes are unclustered
    unclustered_types = cluster_type_counts.get("unclustered", {})
    if unclustered_types:
        unclustered_count = sum(unclustered_types.values())
        dominant_type = max(unclustered_types.items(), key=lambda x: x[1])[0] if unclustered_types else "unknown"
        enriched_clusters.append({
            "id": "unclustered",
            "member_count": unclustered_count,
            "level": 0,
            "type_distribution": unclustered_types,
            "dominant_type": dominant_type,
        })

    # Calculate inter-cluster edges
    cluster_edge_counts: dict[tuple[str, str], int] = {}
    for edge in edges:
        source_cluster = node_to_cluster.get(edge["source"], "unclustered")
        target_cluster = node_to_cluster.get(edge["target"], "unclustered")

        if source_cluster != target_cluster:
            # Normalize edge direction for counting
            pair = tuple(sorted([source_cluster, target_cluster]))
            cluster_edge_counts[pair] = cluster_edge_counts.get(pair, 0) + 1

    cluster_edges = [
        {"source": pair[0], "target": pair[1], "weight": count}
        for pair, count in cluster_edge_counts.items()
        if count > 0
    ]

    log.info(
        "get_hierarchical_graph_complete",
        total_nodes=total_node_count,
        total_edges=total_edge_count,
        displayed_nodes=len(nodes),
        displayed_edges=len(edges),
        clusters=len(enriched_clusters),
        cluster_edges=len(cluster_edges),
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


async def export_to_networkx(client: "GraphClient", organization_id: str) -> Any:
    """Export knowledge graph to NetworkX format.

    Args:
        client: Graph client.
        organization_id: Organization UUID for filtering.

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

    log.info("export_to_networkx_start", org_id=organization_id)

    # Create undirected graph for community detection
    G = nx.Graph()  # noqa: N806

    # Fetch ALL nodes - both Episodic and Entity labels with group_id filter
    node_query = """
    MATCH (n)
    WHERE (n:Episodic OR n:Entity) AND n.group_id = $group_id
    RETURN n.uuid AS id, n.name AS name, n.entity_type AS type
    """

    try:
        node_result = await client.execute_read_org(
            node_query, organization_id, group_id=organization_id
        )

        for record in node_result:
            if isinstance(record, (list, tuple)):
                node_id = record[0] if len(record) > 0 else None
                name = record[1] if len(record) > 1 else ""
                entity_type = record[2] if len(record) > 2 else ""
            else:
                node_id = record.get("id")
                name = record.get("name", "")
                entity_type = record.get("type", "")

            if node_id:
                G.add_node(node_id, name=name, type=entity_type)

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
                G.add_edge(source, target, rel_type=rel_type)

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
    G: Any,  # noqa: N803
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
    G: Any,  # noqa: N803
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
        import igraph as ig
        import leidenalg
    except ImportError as e:
        raise ImportError(
            "leidenalg and igraph are required for Leiden algorithm. "
            "Install with: pip install leidenalg igraph"
        ) from e

    if G.number_of_nodes() == 0:
        return {}, 0.0

    # Convert NetworkX to igraph
    G_ig = ig.Graph.from_networkx(G)  # noqa: N806

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
    G = await export_to_networkx(client, organization_id)  # noqa: N806

    if G.number_of_nodes() < config.min_community_size:
        log.info("detect_communities_too_few_nodes", nodes=G.number_of_nodes())
        return []

    # Select algorithm
    if algorithm == "leiden":
        detect_fn = detect_communities_leiden
    else:
        detect_fn = detect_communities_louvain

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
