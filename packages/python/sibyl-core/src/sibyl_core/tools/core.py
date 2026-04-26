"""Unified tools for Sibyl MCP Server.

This module provides backwards compatibility re-exports. The actual implementations
are now split across focused modules:
- responses.py: Response dataclasses (SearchResult, SearchResponse, etc.)
- helpers.py: Utility functions and constants
- search.py: Unified semantic search
- explore.py: Graph navigation and traversal
- add.py: Entity creation
- health.py: Health checks and statistics
"""

# Re-export everything for backwards compatibility
from sibyl_core.tools.add import add
from sibyl_core.tools.context import compile_context, context_pack_to_dict
from sibyl_core.tools.explore import explore
from sibyl_core.tools.health import get_health, get_stats
from sibyl_core.tools.helpers import (
    MAX_CONTENT_LENGTH,
    MAX_TITLE_LENGTH,
    VALID_ENTITY_TYPES,
    _auto_discover_links,
    _build_entity_metadata,
    _generate_id,
    _get_field,
    _serialize_enum,
    auto_tag_task,
    get_project_tags,
)
from sibyl_core.tools.responses import (
    AddResponse,
    DependencyNode,
    EntitySummary,
    ExploreResponse,
    RelatedEntity,
    SearchResponse,
    SearchResult,
)
from sibyl_core.tools.search import search

__all__ = [
    # Helper functions (for internal use)
    "MAX_CONTENT_LENGTH",
    "MAX_TITLE_LENGTH",
    "VALID_ENTITY_TYPES",
    # Response types
    "AddResponse",
    "DependencyNode",
    "EntitySummary",
    "ExploreResponse",
    "RelatedEntity",
    "SearchResponse",
    "SearchResult",
    "_auto_discover_links",
    "_build_entity_metadata",
    "_generate_id",
    "_get_field",
    "_serialize_enum",
    # Main tools
    "add",
    "auto_tag_task",
    "compile_context",
    "context_pack_to_dict",
    "explore",
    # Health/stats
    "get_health",
    "get_project_tags",
    "get_stats",
    "search",
]
