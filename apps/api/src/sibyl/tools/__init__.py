"""MCP tool implementations.

Sibyl exposes 3 unified tools:
- search: Semantic search across the knowledge graph
- explore: Browse and traverse the graph
- add: Add new knowledge

Plus admin tools for CLI usage (not exposed via MCP):
- health_check, rebuild_indices, get_stats
"""

# Primary unified tools (exposed via MCP)
# Admin tools (CLI only, not exposed via MCP)
from sibyl.tools.admin import (
    HealthStatus,
    RebuildResult,
    get_stats,
    health_check,
    mark_server_started,
    rebuild_indices,
)
from sibyl.tools.core import (
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
    get_stats as get_unified_stats,
    # Tools
    search,
)

__all__ = [
    # Unified tools (MCP)
    "search",
    "explore",
    "add",
    # Response types
    "SearchResponse",
    "SearchResult",
    "ExploreResponse",
    "EntitySummary",
    "RelatedEntity",
    "AddResponse",
    # Resources
    "get_health",
    "get_unified_stats",
    # Admin (CLI only)
    "HealthStatus",
    "RebuildResult",
    "get_stats",
    "health_check",
    "mark_server_started",
    "rebuild_indices",
]
