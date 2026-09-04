"""Data contracts shared by community graph services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sibyl_core.models.entities import Entity, Relationship

GRAPH_RESOLUTION_OVERVIEW = "overview"
GRAPH_RESOLUTION_DETAIL = "detail"


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


@dataclass
class OverviewLevel:
    """The domain map: one aggregate bubble per large community plus the
    edges between them. Carried inside a detail response so a client that
    composes both levels always pairs them from the same community run."""

    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]
    clusters: list[dict[str, Any]]


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
    resolution: str = GRAPH_RESOLUTION_DETAIL
    recommended_resolution: str = GRAPH_RESOLUTION_DETAIL  # server hint for initial mode
    # Present on a whole-graph detail response: the overview built from the
    # same snapshot and clustering, so the two levels cannot disagree.
    overview: OverviewLevel | None = None


@dataclass
class GraphSnapshot:
    """Cached graph snapshot for fast LOD rendering."""

    entities: list[Entity]
    relationships: list[Relationship]
    entity_by_id: dict[str, Entity]


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
