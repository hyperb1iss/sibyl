"""Community detection using Leiden/Louvain algorithm.

Detects hierarchical communities in the knowledge graph for
GraphRAG-style retrieval and summarization.
"""

from __future__ import annotations

import contextlib
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

import structlog

if TYPE_CHECKING:
    from sibyl.graph.client import GraphClient

log = structlog.get_logger()


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


async def export_to_networkx(client: GraphClient) -> Any:
    """Export knowledge graph to NetworkX format.

    Args:
        client: Graph client.

    Returns:
        NetworkX graph object.

    Raises:
        ImportError: If networkx is not installed.
    """
    try:
        import networkx as nx
    except ImportError as e:
        raise ImportError(
            "networkx is required for community detection. Install with: pip install networkx"
        ) from e

    log.info("export_to_networkx_start")

    # Create undirected graph for community detection
    G = nx.Graph()  # noqa: N806

    # Fetch all entities (nodes)
    node_query = """
    MATCH (n:Entity)
    RETURN n.uuid AS id, n.name AS name, n.entity_type AS type
    """

    try:
        node_result = await client.client.driver.execute_query(node_query)

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

    # Fetch all relationships (edges)
    edge_query = """
    MATCH (a:Entity)-[r]->(b:Entity)
    RETURN a.uuid AS source, b.uuid AS target, type(r) AS rel_type
    """

    try:
        edge_result = await client.client.driver.execute_query(edge_query)

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
    G = await export_to_networkx(client)  # noqa: N806

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
            await client.client.driver.execute_query(clear_query)
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
            await client.client.driver.execute_query(
                create_query,
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
                await client.client.driver.execute_query(
                    link_query,
                    entity_id=member_id,
                    community_id=community.id,
                )

    log.info("store_communities_complete", stored=stored)
    return stored


async def get_entity_communities(
    client: GraphClient,
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
        result = await client.client.driver.execute_query(query, entity_id=entity_id)

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
        result = await client.client.driver.execute_query(
            query,
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
