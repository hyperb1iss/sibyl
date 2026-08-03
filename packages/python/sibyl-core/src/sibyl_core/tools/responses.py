"""Response models for Sibyl MCP tools."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal


@dataclass
class SearchResult:
    """A single search result - unified across graph entities and documents."""

    id: str
    type: str
    name: str
    content: str
    score: float
    source: str | None = None
    url: str | None = None
    result_origin: Literal["graph", "document", "raw_memory"] = "graph"
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class SearchResponse:
    """Response from search operation - unified across graph and documents."""

    results: list[SearchResult]
    total: int
    query: str
    filters: dict[str, Any]
    graph_count: int = 0
    document_count: int = 0
    raw_memory_count: int = 0
    limit: int = 10
    offset: int = 0
    has_more: bool = False
    # Client guidance - tells assistants and scripts how to get full content
    usage_hint: str = "Results show previews. To get full content, use: sibyl show <id>"


@dataclass
class EntitySummary:
    """Summary of an entity for listing."""

    id: str
    type: str
    name: str
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class RelatedEntity:
    """An entity related through the graph."""

    id: str
    type: str
    name: str
    relationship: str
    direction: Literal["outgoing", "incoming"]
    distance: int = 1


@dataclass
class ExploreResponse:
    """Response from explore operation."""

    mode: str
    entities: list[EntitySummary] | list[RelatedEntity]
    total: int  # Count of entities returned in this response
    filters: dict[str, Any]
    limit: int = 50  # Results per page
    offset: int = 0  # Current offset
    has_more: bool = False  # True if more results exist beyond the limit
    actual_total: int | None = None  # Actual total count in DB (if available)


@dataclass
class NeighborEntity:
    """One entity reached by a bounded traversal step."""

    id: str
    type: str
    name: str
    relationship: str
    direction: Literal["outgoing", "incoming"]
    distance: int
    score: float
    content: str = ""
    project_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class ExpandNeighborsResponse:
    """Response from one bounded neighbor expansion."""

    origins: list[str]
    neighbors: list[NeighborEntity]
    total: int
    depth: int
    limit: int
    unresolved: list[str] = field(default_factory=list)
    truncated: bool = False
    filters: dict[str, Any] = field(default_factory=dict)
    usage_hint: str = (
        "Bounded traversal step. Widen a hit with fetch_slice, then compose with context. "
        "Stop after three rounds."
    )


@dataclass
class SlicePassage:
    """One span of a sliced memory, or the whole memory when it was never cut."""

    id: str
    name: str
    content: str
    passage_index: int | None = None
    passage_total: int | None = None
    breadcrumb: str | None = None
    truncated: bool = False


@dataclass
class FetchSliceResponse:
    """Response from one slice fetch, with the parent a citation resolves to."""

    entity_id: str
    parent_id: str
    parent_name: str
    parent_type: str
    passages: list[SlicePassage]
    window: int
    sliced: bool
    total: int = 0
    window_start: int | None = None
    passage_total: int | None = None
    covers_parent: bool = False
    project_id: str | None = None
    content_chars: int = 0
    filters: dict[str, Any] = field(default_factory=dict)
    usage_hint: str = (
        "Cite the parent memory, not the span. Compose the final answer with context. "
        "Stop after three rounds."
    )


@dataclass
class ConflictWarning:
    """A potential contradiction detected during ingest.

    When adding new knowledge, we check for semantically similar existing facts
    that may contradict the new information.
    """

    existing_id: str
    existing_name: str
    existing_content: str
    similarity_score: float
    conflict_type: Literal["semantic_overlap", "potential_contradiction", "duplicate"]
    explanation: str | None = None


@dataclass
class AddResponse:
    """Response from add operation."""

    success: bool
    id: str | None
    message: str
    timestamp: datetime
    conflicts: list[ConflictWarning] = field(default_factory=list)
    background_jobs: dict[str, Any] = field(default_factory=dict)
    # Per-probe rank-or-absent, present only when the write carried probes.
    probe_rehearsal: dict[str, Any] | None = None


@dataclass
class DependencyNode:
    """A node in a dependency graph."""

    id: str
    name: str
    status: str
    blocking: list[str]
    blocked_by: list[str]


@dataclass
class TemporalEdge:
    """An edge with bi-temporal metadata.

    Bi-temporal model:
    - created_at/expired_at: System time (when we learned/invalidated this)
    - valid_at/invalid_at: Real-world time (when fact was/ceased to be true)
    """

    id: str
    name: str  # Relationship name / fact
    source_id: str
    source_name: str
    target_id: str
    target_name: str
    # System time: when we knew about this
    created_at: datetime | None = None
    expired_at: datetime | None = None  # If set, this edge has been invalidated
    # Real-world time: when the fact was true
    valid_at: datetime | None = None
    invalid_at: datetime | None = None
    # Additional context
    fact: str | None = None
    is_current: bool = True  # False if superseded by newer info


@dataclass
class TemporalResponse:
    """Response from temporal query operations."""

    mode: Literal["history", "timeline", "conflicts"]
    entity_id: str | None
    edges: list[TemporalEdge]
    total: int
    as_of: datetime | None = None  # Point-in-time filter if used
    message: str | None = None
