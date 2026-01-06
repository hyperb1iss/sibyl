"""MCP tool implementations.

Sibyl exposes 4 unified tools:
- search: Semantic search across the knowledge graph
- explore: Browse and traverse the graph
- temporal_query: Bi-temporal queries (history, timeline, conflicts)
- add: Add new knowledge

Plus admin tools for CLI usage (not exposed via MCP):
- health_check, rebuild_indices, get_stats
"""

# Primary unified tools (exposed via MCP)
# Admin tools (CLI only, not exposed via MCP)
from sibyl_core.tools.admin import (
    HealthStatus,
    RebuildResult,
    get_stats,
    health_check,
    mark_server_started,
    rebuild_indices,
)
from sibyl_core.tools.conflicts import detect_conflicts, find_similar_entities
from sibyl_core.tools.core import (
    AddResponse,
    EntitySummary,
    ExploreResponse,
    RelatedEntity,
    # Response types
    SearchResponse,
    SearchResult,
    add,
    explore,
    # Resources
    get_health,
    # Tools
    search,
)
from sibyl_core.tools.core import (
    get_stats as get_unified_stats,
)
from sibyl_core.tools.responses import ConflictWarning, TemporalEdge, TemporalResponse
from sibyl_core.tools.temporal import (
    find_conflicts as find_temporal_conflicts,
)
from sibyl_core.tools.temporal import (
    get_entity_history,
    temporal_query,
)

__all__ = [
    "AddResponse",
    # Conflict detection
    "ConflictWarning",
    "EntitySummary",
    "ExploreResponse",
    # Admin (CLI only)
    "HealthStatus",
    "RebuildResult",
    "RelatedEntity",
    # Response types
    "SearchResponse",
    "SearchResult",
    # Temporal response types
    "TemporalEdge",
    "TemporalResponse",
    "add",
    # Conflict detection functions
    "detect_conflicts",
    "explore",
    # Temporal queries (renamed to avoid collision)
    "find_similar_entities",
    "find_temporal_conflicts",
    # Resources
    "get_entity_history",
    "get_health",
    "get_stats",
    "get_unified_stats",
    "health_check",
    "mark_server_started",
    "rebuild_indices",
    # Unified tools (MCP)
    "search",
    "temporal_query",
]
