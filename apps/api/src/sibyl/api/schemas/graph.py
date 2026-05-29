"""Graph visualization request/response models."""

from typing import Any

from pydantic import BaseModel, Field

from sibyl_core.models.entities import RelationshipType


class GraphNode(BaseModel):
    """Node for graph visualization."""

    id: str = Field(..., description="Unique node ID")
    type: str = Field(..., description="Entity type")
    label: str = Field(..., description="Display label")
    color: str = Field(..., description="Node color (hex)")
    size: float = Field(default=1.0, description="Relative node size")
    x: float | None = Field(default=None, description="X position (if pre-computed)")
    y: float | None = Field(default=None, description="Y position (if pre-computed)")
    metadata: dict[str, Any] = Field(default_factory=dict)


class GraphEdge(BaseModel):
    """Edge for graph visualization."""

    id: str = Field(..., description="Unique edge ID")
    source: str = Field(..., description="Source node ID")
    target: str = Field(..., description="Target node ID")
    type: str = Field(..., description="Relationship type")
    label: str = Field(default="", description="Edge label")
    weight: float = Field(default=1.0, description="Edge weight/thickness")


class GraphData(BaseModel):
    """Full graph data for visualization."""

    nodes: list[GraphNode]
    edges: list[GraphEdge]
    node_count: int
    edge_count: int


class SubgraphRequest(BaseModel):
    """Request for subgraph around an entity."""

    entity_id: str = Field(..., description="Center entity ID")
    depth: int = Field(default=2, ge=1, le=4, description="Traversal depth")
    relationship_types: list[RelationshipType] | None = Field(
        default=None, description="Filter relationship types"
    )
    max_nodes: int = Field(default=100, ge=1, le=500, description="Maximum nodes to return")
