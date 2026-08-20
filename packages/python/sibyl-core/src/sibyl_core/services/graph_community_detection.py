"""Community detection algorithms and hierarchy linking."""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import structlog

from sibyl_core.services.graph_community_managers import (
    _list_all_entities,
    _list_all_relationships,
)
from sibyl_core.services.graph_community_models import (
    CommunityConfig,
    DetectedCommunity,
    GraphSnapshot,
)
from sibyl_core.services.graph_community_selection import _snapshot_to_networkx
from sibyl_core.services.graph_community_snapshot import _get_graph_snapshot

log = structlog.get_logger()


def _detect_communities_from_graph(
    G: Any,
    *,
    config: CommunityConfig,
    algorithm: str,
) -> list[DetectedCommunity]:
    if G.number_of_nodes() < config.min_community_size:
        log.info("detect_communities_too_few_nodes", nodes=G.number_of_nodes())
        return []

    detect_fn = detect_communities_leiden if algorithm == "leiden" else detect_communities_louvain
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

    all_communities = link_hierarchy(all_level_communities)

    log.info(
        "detect_communities_complete",
        total_communities=len(all_communities),
        levels=len(all_level_communities),
    )

    return all_communities


async def export_to_networkx(
    client: Any,
    organization_id: str,
    type_affinity_weight: float = 2.0,
    *,
    max_entities: int | None = None,
    max_relationships: int | None = None,
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
    log.info("export_to_networkx_start", org_id=organization_id, type_affinity=type_affinity_weight)

    if max_entities is None and max_relationships is None:
        log.info("export_to_networkx_unbounded_materialization", org_id=organization_id)
        try:
            entities = await _list_all_entities(client, organization_id)
        except Exception as e:
            log.warning("export_nodes_failed", error=str(e))
            entities = []

        try:
            relationships = await _list_all_relationships(client, organization_id)
        except Exception as e:
            log.warning("export_edges_failed", error=str(e))
            relationships = []
    else:
        try:
            snapshot = await _get_graph_snapshot(
                client,
                organization_id,
                max_entities=max_entities,
                max_relationships=max_relationships,
            )
        except Exception as e:
            log.warning("export_graph_snapshot_failed", error=str(e))
            snapshot = GraphSnapshot(entities=[], relationships=[], entity_by_id={})
        entities = snapshot.entities
        relationships = snapshot.relationships

    G = _snapshot_to_networkx(
        entities,
        relationships,
        type_affinity_weight=type_affinity_weight,
    )

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
    client: Any,
    organization_id: str,
    config: CommunityConfig | None = None,
    algorithm: str = "louvain",
    *,
    max_entities: int | None = None,
    max_relationships: int | None = None,
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
    G = await export_to_networkx(
        client,
        organization_id,
        max_entities=max_entities,
        max_relationships=max_relationships,
    )
    return await asyncio.to_thread(
        _detect_communities_from_graph,
        G,
        config=config,
        algorithm=algorithm,
    )
