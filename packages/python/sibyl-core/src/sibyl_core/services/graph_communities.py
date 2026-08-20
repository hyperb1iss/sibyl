"""Public community graph contracts.

Implementation lives in responsibility-specific modules. Private helpers stay
with their canonical owners so this facade remains an intentional public API.
"""

from sibyl_core.services.graph_community_clusters import (
    CLUSTER_CACHE,
    CLUSTER_CACHE_TTL,
    get_cluster_nodes,
    get_clusters_for_visualization,
)
from sibyl_core.services.graph_community_detection import (
    detect_communities,
    detect_communities_leiden,
    detect_communities_louvain,
    export_to_networkx,
    link_hierarchy,
    partition_to_communities,
)
from sibyl_core.services.graph_community_hierarchy import (
    GRAPH_LOD_CACHE,
    GRAPH_LOD_CACHE_TTL,
    HIERARCHICAL_CACHE,
    HIERARCHICAL_CACHE_TTL,
    get_hierarchical_graph,
)
from sibyl_core.services.graph_community_membership import (
    get_community_members,
    get_entity_communities,
    store_communities,
)
from sibyl_core.services.graph_community_models import (
    GRAPH_RESOLUTION_DETAIL,
    GRAPH_RESOLUTION_OVERVIEW,
    ClusterSummary,
    CommunityConfig,
    DetectedCommunity,
    GraphSnapshot,
    HierarchicalGraphData,
)
from sibyl_core.services.graph_community_selection import (
    CLUSTER_DETAIL_MAX_EDGES,
    CLUSTER_DETAIL_MAX_NODES,
    CLUSTER_SUMMARY_MEMBER_SAMPLE,
    DETECTION_MAX_ENTITIES,
    DETECTION_MAX_RELATIONSHIPS,
    OVERVIEW_MAX_CLUSTERS,
    OVERVIEW_NODE_THRESHOLD,
)
from sibyl_core.services.graph_community_snapshot import (
    GRAPH_SNAPSHOT_CACHE,
    GRAPH_SNAPSHOT_CACHE_TTL,
    GRAPH_SNAPSHOT_LOADS,
)

__all__ = [
    "CLUSTER_CACHE",
    "CLUSTER_CACHE_TTL",
    "CLUSTER_DETAIL_MAX_EDGES",
    "CLUSTER_DETAIL_MAX_NODES",
    "CLUSTER_SUMMARY_MEMBER_SAMPLE",
    "DETECTION_MAX_ENTITIES",
    "DETECTION_MAX_RELATIONSHIPS",
    "GRAPH_LOD_CACHE",
    "GRAPH_LOD_CACHE_TTL",
    "GRAPH_RESOLUTION_DETAIL",
    "GRAPH_RESOLUTION_OVERVIEW",
    "GRAPH_SNAPSHOT_CACHE",
    "GRAPH_SNAPSHOT_CACHE_TTL",
    "GRAPH_SNAPSHOT_LOADS",
    "HIERARCHICAL_CACHE",
    "HIERARCHICAL_CACHE_TTL",
    "OVERVIEW_MAX_CLUSTERS",
    "OVERVIEW_NODE_THRESHOLD",
    "ClusterSummary",
    "CommunityConfig",
    "DetectedCommunity",
    "GraphSnapshot",
    "HierarchicalGraphData",
    "detect_communities",
    "detect_communities_leiden",
    "detect_communities_louvain",
    "export_to_networkx",
    "get_cluster_nodes",
    "get_clusters_for_visualization",
    "get_community_members",
    "get_entity_communities",
    "get_hierarchical_graph",
    "link_hierarchy",
    "partition_to_communities",
    "store_communities",
]
